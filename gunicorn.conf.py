"""Explicit gunicorn settings for AeroTrack Transit.

Root cause of the "detail: starting" bug that never updates
--------------------------------------------------------------
Render's deploy logs showed app.py being imported, and the MQTT/serial
ingestion threads being started, at the gunicorn MASTER/arbiter's pid --
*before* the arbiter even logged "Starting gunicorn" and before it forked
off the worker process that actually serves HTTP requests. That ordering
is only possible when `preload_app` is active: gunicorn imports the WSGI
app once in the master, then forks worker processes from that already-
loaded state.

fork() only carries the thread that calls it into the child process --
every other running thread, including our `_mqtt_worker` / `_serial_worker`
daemon threads, simply does not exist in the forked worker. Those threads
keep running, but only inside the master process, which never answers a
single HTTP request. The worker that serves `/api/status` is left holding
a frozen, never-updated snapshot of the tracker's transport state from the
instant of fork -- which is exactly why `detail` stays stuck on "starting"
forever, even though the startup logs prove the threads are alive.

We could not find where preload_app was being turned on (no --preload flag
in the Start Command, no GUNICORN_CMD_ARGS, no Secret Files), so this file
does two things instead of chasing the mystery source further:

1. Declares `preload_app = False` explicitly and authoritatively, since an
   explicit config file passed via `-c` takes precedence over environment-
   based defaults.
2. Adds a `post_fork` hook as a safety net: if preload_app is somehow still
   active despite (1), this restarts ingestion fresh inside each worker
   process, right after fork, so the threads that mutate the tracker run in
   the same process that actually serves requests.

IMPORTANT: for this file to take effect, the Render Start Command must load
it explicitly, e.g.:

    gunicorn -c gunicorn.conf.py --chdir backend --workers 1 --threads 4 \
        --timeout 120 --bind 0.0.0.0:$PORT app:app
"""
import os

preload_app = False


def post_fork(server, worker):
    app = getattr(worker.app, "callable", None)
    if app is None:
        # preload_app is off, as intended: this worker hasn't imported
        # app.py yet. It will do so in a moment via its own init_process(),
        # which runs the normal top-level `ingestion.start()` call inside
        # this same worker process. Nothing to do here.
        return

    ingestion = app.extensions.get("aerotrack_ingestion")
    if ingestion is None:
        return

    # The Thread objects on this object were created pre-fork in the
    # master and do not exist in this process; discard them and start
    # fresh ones bound to this worker.
    server.log.info("post_fork: preload_app produced stale ingestion threads; restarting in worker pid=%s", os.getpid())
    ingestion.threads = []
    ingestion.stop_event.clear()
    ingestion.start()
