from __future__ import print_function
from abc import ABCMeta, abstractmethod
from threading import RLock
from datetime import datetime, timedelta
from logging import getLogger
from uuid import uuid4
import sys

from dateutil.tz import tzlocal
import six

from apscheduler.schedulers import SchedulerAlreadyRunningError, SchedulerNotRunningError
from apscheduler.util import *
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.job import Job, MaxInstancesReachedError, JobHandle
from apscheduler.events import *
from apscheduler.threadpool import ThreadPool

try:
    from inspect import getfullargspec as getargspec
except ImportError:
    from inspect import getargspec


class BaseScheduler(six.with_metaclass(ABCMeta)):
    """Base class for all schedulers."""

    _stopped = True

    #
    # Public API
    #

    def __init__(self, gconfig={}, **options):
        super(BaseScheduler, self).__init__()
        self._jobstores = {}
        self._jobstores_lock = self._create_lock()
        self._listeners = []
        self._listeners_lock = self._create_lock()
        self._pending_jobs = []
        self._triggers = {}
        self.configure(gconfig, **options)

    def configure(self, gconfig={}, **options):
        """
        Reconfigures the scheduler with the given options. Can only be done when the scheduler isn't running.
        """
        if self.running:
            raise SchedulerAlreadyRunningError

        config = combine_opts(gconfig, 'apscheduler.', options)
        self._configure(config)

    @abstractmethod
    def start(self):
        """Starts the scheduler. The details of this process depend on the implementation."""

        if self.running:
            raise SchedulerAlreadyRunningError

        # Create a RAMJobStore as the default if there is no default job store
        if not 'default' in self._jobstores:
            self.add_jobstore(MemoryJobStore(), 'default', True)

        # Schedule all pending jobs
        for job, jobstore in self._pending_jobs:
            self._real_add_job(job, jobstore, False)
        del self._pending_jobs[:]

        self._stopped = False
        self.logger.info('Scheduler started')

        # Notify listeners that the scheduler has been started
        self._notify_listeners(SchedulerEvent(EVENT_SCHEDULER_START))

    @abstractmethod
    def shutdown(self, wait=True):
        """
        Shuts down the scheduler. Does not interrupt any currently running jobs.

        :param wait: ``True`` to wait until all currently executing jobs have finished
        """
        if not self.running:
            raise SchedulerNotRunningError

        self._stopped = True

        # Shut down the thread pool
        self._threadpool.shutdown(wait)

        # Close all job stores
        for jobstore in six.itervalues(self._jobstores):
            jobstore.close()

        self.logger.info('Scheduler has been shut down')
        self._notify_listeners(SchedulerEvent(EVENT_SCHEDULER_SHUTDOWN))

    @property
    def running(self):
        return not self._stopped

    def add_jobstore(self, jobstore, alias, quiet=False):
        """
        Adds a job store to this scheduler.

        :param jobstore: job store to be added
        :param alias: alias for the job store
        :param quiet: True to suppress scheduler thread wakeup
        :type jobstore: instance of :class:`~apscheduler.jobstores.base.JobStore`
        :type alias: str
        """
        with self._jobstores_lock:
            if alias in self._jobstores:
                raise KeyError('Alias "%s" is already in use' % alias)
            self._jobstores[alias] = jobstore

        # Notify listeners that a new job store has been added
        self._notify_listeners(JobStoreEvent(EVENT_JOBSTORE_ADDED, alias))

        # Notify the scheduler so it can scan the new job store for jobs
        if not quiet and self.running:
            self._wakeup()

    def remove_jobstore(self, alias, close=True):
        """
        Removes the job store by the given alias from this scheduler.

        :param close: ``True`` to close the job store after removing it
        :type alias: str
        """
        with self._jobstores_lock:
            jobstore = self._jobstores.pop(alias)
            if not jobstore:
                raise KeyError('No such job store: %s' % alias)

        # Close the job store if requested
        if close:
            jobstore.close()

        # Notify listeners that a job store has been removed
        self._notify_listeners(JobStoreEvent(EVENT_JOBSTORE_REMOVED, alias))

    def add_listener(self, callback, mask=EVENT_ALL):
        """
        Adds a listener for scheduler events. When a matching event occurs, ``callback`` is executed with the event
        object as its sole argument. If the ``mask`` parameter is not provided, the callback will receive events of all
        types.

        :param callback: any callable that takes one argument
        :param mask: bitmask that indicates which events should be listened to
        """
        with self._listeners_lock:
            self._listeners.append((callback, mask))

    def remove_listener(self, callback):
        """
        Removes a previously added event listener.
        """
        with self._listeners_lock:
            for i, (cb, _) in enumerate(self._listeners):
                if callback == cb:
                    del self._listeners[i]

    def add_job(self, trigger, func, args=None, kwargs=None, id=None, name=None, misfire_grace_time=None, coalesce=None,
                max_runs=None, max_instances=1, jobstore='default', **trigger_args):
        """
        Adds the given job to the job list and notifies the scheduler thread.

        The ``func`` argument can be given either as a callable object or a textual reference in the
        ``package.module:some.object`` format, where the first half (separated by ``:``) is an importable module and the
        second half is a reference to the callable object, relative to the module.

        The ``trigger`` argument can either be:

        # the plugin name of the trigger (e.g. "cron"), in which case any extra keyword arguments to this method are
          passed on to the trigger's constructor
        # an instance of a trigger class

        :param trigger: trigger that determines when ``func`` is called
        :param func: callable (or a textual reference to one) to run at the given time
        :param args: list of positional arguments to call func with
        :param kwargs: dict of keyword arguments to call func with
        :param id: explicit identifier for the job (for modifying it later)
        :param name: textual description of the job
        :param misfire_grace_time: seconds after the designated run time that the job is still allowed to be run
        :param coalesce: run once instead of many times if the scheduler determines that the job should be run more than
                         once in succession
        :param max_runs: maximum number of times this job is allowed to be triggered
        :param max_instances: maximum number of concurrently running instances allowed for this job
        :param jobstore: alias of the job store to store the job in
        :type id: str/unicode
        :type args: list/tuple
        :type jobstore: str/unicode
        :type misfire_grace_time: int
        :type kwargs: dict
        :type coalesce: bool
        :type max_runs: int
        :type max_instances: int
        :rtype: :class:`~apscheduler.job.JobHandle`
        """

        trigger_args.setdefault('timezone', self.timezone)

        job_kwargs = {
            'trigger': trigger,
            'trigger_args': trigger_args,
            'func': func,
            'args': tuple(args) if args is not None else (),
            'kwargs': dict(kwargs) if kwargs is not None else {},
            'id': id,
            'name': name,
            'misfire_grace_time': misfire_grace_time if misfire_grace_time is not None else self.misfire_grace_time,
            'coalesce': coalesce if coalesce is not None else self.coalesce,
            'max_runs': max_runs,
            'max_instances': max_instances
        }
        job = Job(**job_kwargs)

        # Don't really add jobs to job stores before the scheduler is up and running
        if not self.running:
            self._pending_jobs.append((job, jobstore))
            self.logger.info('Adding job tentatively -- it will be properly scheduled when the scheduler starts')
        else:
            self._real_add_job(job, jobstore, True)

        return JobHandle(self, jobstore, job)

    def scheduled_job(self, trigger, args=None, kwargs=None, id=None, name=None, misfire_grace_time=None, coalesce=None,
                      max_runs=None, max_instances=1, jobstore='default', **trigger_args):
        """A decorator version of :meth:`add_job`."""

        def inner(func):
            self.add_job(trigger, func, args, kwargs, id, misfire_grace_time, coalesce, name, max_runs,
                         max_instances, jobstore, **trigger_args)
            return func
        return inner

    def modify_job(self, job_id, jobstore='default', **changes):
        """
        Modifies the properties of a single job. Modifications are passed to this method as extra keyword arguments.

        :param job_id: the identifier of the job
        :param jobstore: alias of the job store
        """

        with self._jobstores_lock:
            # Check if the job is among the pending jobs
            for job, store in self._pending_jobs:
                if job.id == job_id:
                    job.modify(changes)
                    return
            else:
                store = self._jobstores[jobstore]
                job = store.lookup_job(changes.get('id', job_id))
                changes = job.validate_changes(changes)
                store.modify_job(job_id, changes)

        self._notify_listeners(JobStoreEvent(EVENT_JOBSTORE_JOB_MODIFIED, jobstore, job_id))

        # Wake up the scheduler since the job's next run time may have been changed
        self._wakeup()

    def get_jobs(self, jobstore=None, pending=None):
        """
        Returns a list of pending jobs (if the scheduler hasn't been started yet) and scheduled jobs,
        either from a specific job store or from all of them.

        :param jobstore: alias of the job store
        :param pending: ``False`` to leave out pending jobs (jobs that are waiting for the scheduler start to be added
                        to their respective job stores), ``True`` to only include pending jobs, anything else to return
                        both
        :return: list of :class:`~apscheduler.job.JobHandle` objects
        """

        with self._jobstores_lock:
            jobs = []

            if pending is not False:
                for job, alias in self._pending_jobs:
                    if jobstore is None or alias == jobstore:
                        jobs.append(JobHandle(self, alias, job))

            if pending is not True:
                jobstores = [jobstore] if jobstore else self._jobstores
                for alias, store in six.iteritems(jobstores):
                    for job in store.get_all_jobs():
                        jobs.append(JobHandle(self, alias, job))

            return jobs

    def get_job(self, job_id, jobstore='default'):
        """Returns a JobHandle for the specified job."""

        with self._jobstores_lock:
            job = self._jobstores[jobstore].lookup_job(job_id)
            return JobHandle(self, jobstore, job)

    def remove_job(self, job_id, jobstore='default'):
        """
        Removes a job, preventing it from being run any more.

        :param job_id: the identifier of the job
        :param jobstore: alias of the job store
        """

        with self._jobstores_lock:
            # Check if the job is among the pending jobs
            for i, (job, store) in enumerate(self._pending_jobs):
                if job.id == job_id:
                    del self._pending_jobs[i]
                    return

            self._jobstores[jobstore].remove_job(job_id)

        # Notify listeners that a job has been removed
        event = JobStoreEvent(EVENT_JOBSTORE_JOB_REMOVED, jobstore, job_id)
        self._notify_listeners(event)

        self.logger.info('Removed job %s', job_id)

    def remove_all_jobs(self, jobstore=None):
        """
        Removes all jobs from the specified job store, or all job stores if none is given.

        :param jobstore: alias of the job store
        """

        with self._jobstores_lock:
            jobstores = [jobstore] if jobstore else self._jobstores
            for alias in jobstores:
                self._jobstores[alias].remove_all_jobs()

    def print_jobs(self, jobstore=None, out=None):
        """
        Prints out a textual listing of all jobs currently scheduled on either all job stores or just a specific one.

        :param jobstore: alias of the job store
        :param out: a file-like object to print to (defaults to **sys.stdout** if nothing is given)
        """
        out = out or sys.stdout
        with self._jobstores_lock:
            jobs = self.get_jobs(jobstore, True)
            if jobs:
                print(six.u('Pending jobs:'), file=out)
                for job in jobs:
                    print(six.u('    %s') % job, file=out)

            for alias, store in six.iteritems(self._jobstores):
                if jobstore is None or alias == jobstore:
                    print(six.u('Jobstore %s:') % alias, file=out)
                    jobs = self.get_jobs(jobstore, False)
                    if jobs:
                        for job in jobs:
                            print(six.u('    %s') % job, file=out)
                    else:
                        print(six.u('    No scheduled jobs'), file=out)

    #
    # Protected API
    #

    def _configure(self, config):
        # Set general options
        self.logger = maybe_ref(config.pop('logger', None)) or getLogger('apscheduler')
        self.misfire_grace_time = int(config.pop('misfire_grace_time', 1))
        self.coalesce = asbool(config.pop('coalesce', True))
        self.timezone = astimezone(config.pop('timezone', None)) or tzlocal()

        # Configure the thread pool
        if 'threadpool' in config:
            self._threadpool = maybe_ref(config['threadpool'])
        else:
            threadpool_opts = combine_opts(config, 'threadpool.')
            self._threadpool = ThreadPool(**threadpool_opts)

        # Configure job stores
        jobstore_opts = combine_opts(config, 'jobstore.')
        jobstores = {}
        for key, value in jobstore_opts.items():
            store_name, option = key.split('.', 1)
            opts_dict = jobstores.setdefault(store_name, {})
            opts_dict[option] = value

        for alias, opts in jobstores.items():
            classname = opts.pop('class')
            cls = maybe_ref(classname)
            jobstore = cls(**opts)
            self.add_jobstore(jobstore, alias, True)

    def _notify_listeners(self, event):
        with self._listeners_lock:
            listeners = tuple(self._listeners)

        for cb, mask in listeners:
            if event.code & mask:
                try:
                    cb(event)
                except:
                    self.logger.exception('Error notifying listener')

    def _real_add_job(self, job, jobstore, wakeup):
        # Recalculate the next run time
        job.next_run_time = job.trigger.get_next_fire_time(self._current_time())

        # Add the job to the given job store
        store = self._jobstores.get(jobstore)
        if not store:
            raise KeyError('No such job store: %s' % jobstore)
        store.add_job(job)

        # Notify listeners that a new job has been added
        event = JobStoreEvent(EVENT_JOBSTORE_JOB_ADDED, jobstore, job.id)
        self._notify_listeners(event)

        self.logger.info('Added job "%s" to job store "%s"', job, jobstore)

        # Notify the scheduler about the new job
        if wakeup:
            self._wakeup()

    @abstractmethod
    def _wakeup(self):
        """Triggers :meth:`_process_jobs` to be run in an implementation specific manner."""

    def _create_lock(self):
        return RLock()

    def _current_time(self):
        return datetime.now(self.timezone)

    def _run_job(self, job, run_times):
        """Acts as a harness that runs the actual job code in the thread pool."""

        for run_time in run_times:
            # See if the job missed its run time window, and handle possible
            # misfires accordingly
            difference = self._current_time() - run_time
            grace_time = timedelta(seconds=job.misfire_grace_time)
            if difference > grace_time:
                # Notify listeners about a missed run
                event = JobEvent(EVENT_JOB_MISSED, job, run_time)
                self._notify_listeners(event)
                self.logger.warning('Run time of job "%s" was missed by %s', job, difference)
            else:
                try:
                    job.add_instance()
                except MaxInstancesReachedError:
                    event = JobEvent(EVENT_JOB_MISSED, job, run_time)
                    self._notify_listeners(event)
                    self.logger.warning(
                        'Execution of job "%s" skipped: maximum number of running instances reached (%d)', job,
                        job.max_instances)
                    break

                self.logger.info('Running job "%s" (scheduled at %s)', job, run_time)

                try:
                    retval = job.func(*job.args, **job.kwargs)
                except:
                    # Notify listeners about the exception
                    exc, tb = sys.exc_info()[1:]
                    event = JobEvent(EVENT_JOB_ERROR, job, run_time, exception=exc, traceback=tb)
                    self._notify_listeners(event)

                    self.logger.exception('Job "%s" raised an exception', job)
                else:
                    # Notify listeners about successful execution
                    event = JobEvent(EVENT_JOB_EXECUTED, job, run_time, retval=retval)
                    self._notify_listeners(event)

                    self.logger.info('Job "%s" executed successfully', job)

                job.remove_instance()

                # If coalescing is enabled, don't attempt any further runs
                if job.coalesce:
                    break

    def _process_jobs(self):
        """Iterates through jobs in every jobstore, starts jobs that are due and figures out how long to wait for
        the next round.
        """

        self.logger.debug('Looking for jobs to run')
        now = self._current_time()
        next_wakeup_time = None

        with self._jobstores_lock:
            for alias, jobstore in six.iteritems(self._jobstores):
                jobs, jobstore_next_wakeup_time = jobstore.get_pending_jobs(now)
                if not next_wakeup_time:
                    next_wakeup_time = jobstore_next_wakeup_time
                elif jobstore_next_wakeup_time:
                    next_wakeup_time = min(next_wakeup_time or jobstore_next_wakeup_time)

                for job in jobs:
                    run_times = job.get_run_times(now)
                    if run_times:
                        self._threadpool.submit(self._run_job, job, run_times)

                        # Update the job, but don't keep finished jobs around
                        job_runs = job.runs + 1 if job.coalesce else len(run_times)
                        job_next_run = job.trigger.get_next_fire_time(now + timedelta(microseconds=1))
                        if job_next_run and (job.max_runs is None or job_runs < job.max_runs):
                            changes = {'next_run_time': job_next_run, 'runs': job_runs}
                            jobstore.modify_job(job.id, changes)
                        else:
                            self.remove_job(job.id, alias)

                    if not next_wakeup_time:
                        next_wakeup_time = job.next_run_time
                    elif job.next_run_time:
                        next_wakeup_time = min(next_wakeup_time, job.next_run_time)

        # Determine the delay until this method should be called again
        if next_wakeup_time is not None:
            wait_seconds = time_difference(next_wakeup_time, now)
            self.logger.debug('Next wakeup is due at %s (in %f seconds)', next_wakeup_time, wait_seconds)
        else:
            wait_seconds = None
            self.logger.debug('No jobs; waiting until a job is added')

        return wait_seconds