"""Polls ADO for fixie-tagged tickets and runs them, one worker thread per ticket, up to a pool limit."""

import html
import logging
import shutil
import tempfile
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from fixie import helper, publisher, runner
from fixie.allowlist import find_tagger, is_allowed
from fixie.classify import classify
from fixie.events import load_snapshot
from fixie.gateway import AdoGateway
from fixie.helper import HelperConfig
from fixie.model import BOT_SIGN, Action, RunStatus
from fixie.render import parse_base_branch, write_input
from fixie.runner import RunnerError
from fixie.settings import Settings
from fixie.status_comment import find_status_comment, trusted_status

logger = logging.getLogger(__name__)

UNAUTHORIZED_MARKER = "fixie-unauthorized"
RUN_ACTIONS = (Action.NEW, Action.RESUME, Action.REBUILD, Action.RETRY)
_START_ACTIONS = (Action.NEW, Action.REBUILD)


def _unauthorized_text(tag: str) -> str:
    return (
        f'<p>{BOT_SIGN} fixie: this ticket was tagged "{html.escape(tag)}" by someone who is not on '
        f"fixie's allowlist, so fixie removed the tag and will not work on it. "
        f"Ask an allowlisted user to re-add the tag.</p>\n"
        f"<p><code>{UNAUTHORIZED_MARKER}</code></p>"
    )


class Orchestrator:
    """Polls ADO, classifies tickets and runs at most max_parallel tickets at once in worker threads."""

    def __init__(
        self,
        gateway: AdoGateway,
        settings: Settings,
        *,
        helper_config: HelperConfig | None = None,
        run_ticket=runner.run_ticket,
        refresh_mirror=helper.create_or_refresh_mirror,
        publish=publisher.publish,
        delete_volume=runner.delete_ticket_volume,
        kill_ticket=runner.kill_ticket,
        running_tickets=runner.running_tickets,
        ticket_volumes=runner.ticket_volumes,
    ):
        self.gateway = gateway
        self.settings = settings
        self._helper_config = helper_config or HelperConfig.from_settings(settings)
        self._run_ticket = run_ticket
        self._refresh_mirror = refresh_mirror
        self._publish = publish
        self._delete_volume = delete_volume
        self._kill_ticket = kill_ticket
        self._running_tickets = running_tickets
        self._ticket_volumes = ticket_volumes

        self._in_flight: set[int] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=settings.max_parallel, thread_name_prefix="fixie-worker")

    def recover(self) -> None:
        """Kill every container left over from a previous run, labelled by ticket id, before polling."""
        ids = {t for t in (self._running_tickets() | self._ticket_volumes()) if t.isascii() and t.isdigit()}
        logger.info(f"orchestrator: startup sweep over {len(ids)} tickets")
        for tid in sorted(ids, key=int):
            self._kill_ticket(tid)

    def _reject_unauthorized(self, tid: int, tag: str) -> None:
        self.gateway.remove_tag(tid, tag)
        self.gateway.add_comment(tid, _unauthorized_text(tag))

    def _submit(self, tid: int, snapshot, status, classification) -> Future | None:
        with self._lock:
            if len(self._in_flight) >= self.settings.max_parallel:
                logger.info(f"orchestrator: ticket {tid} deferred, pool full")
                return None
            self._in_flight.add(tid)
        try:
            return self._pool.submit(self._work, snapshot, status, classification)
        except Exception:
            with self._lock:
                self._in_flight.discard(tid)
            raise

    def poll_once(self) -> list[Future]:
        """Classify every candidate ticket and hand eligible ones to workers; returns the submitted futures."""
        gw, s = self.gateway, self.settings
        tagged = gw.find_tagged_tickets(s.ticket_tag)
        ids = tagged + sorted(
            {int(t) for t in self._ticket_volumes() if t.isascii() and t.isdigit()} - set(tagged)
        )
        futures: list[Future] = []
        for tid in ids:
            try:
                with self._lock:
                    in_flight = tid in self._in_flight
                if in_flight:
                    logger.info(f"orchestrator: ticket {tid} in flight, skipped")
                    continue

                snapshot = load_snapshot(gw, tid)
                status = trusted_status(
                    find_status_comment(snapshot.comments, s.owner_email), tid, s.owner_email, s.state_secret
                )
                classification = classify(snapshot, status, s)
                logger.info(f"orchestrator: ticket {tid} classified {classification.action}")
                action = classification.action

                if action in RUN_ACTIONS:
                    tagger = find_tagger(snapshot.updates, s.ticket_tag)
                    if not is_allowed(tagger, s.allowlist, s.owner_email):
                        logger.info(f"orchestrator: ticket {tid} tagged by non-allowlisted user, tag removed")
                        self._reject_unauthorized(tid, s.ticket_tag)
                        continue
                    if self._stop.is_set():
                        continue
                    future = self._submit(tid, snapshot, status, classification)
                    if future is not None:
                        futures.append(future)
                elif action == Action.GIVE_UP:
                    if status.result != RunStatus.GAVE_UP:
                        publisher.record_give_up(gw, s, tid, status)
                elif action == Action.CLEAN_UP:
                    self._delete_volume(str(tid))
                    logger.info(f"orchestrator: ticket {tid} volume deleted")
            except Exception as exc:
                logger.warning(f"orchestrator: ticket {tid} poll failed: {type(exc).__name__}: {exc}")
        return futures

    def _work(self, snapshot, status, classification) -> None:
        tid = snapshot.ticket.id
        tmp: Path | None = None
        try:
            started = publisher.mark_run_started(self.gateway, self.settings, tid, status)
            tmp = Path(tempfile.mkdtemp(prefix=f"fixie-in-{tid}-"))
            write_input(self.gateway, snapshot, classification.events, self.settings, tmp)

            if self._stop.is_set():
                logger.info(f"orchestrator: ticket {tid} stopping, run skipped")
                return

            with helper.MIRROR_LOCK:
                self._refresh_mirror(self._helper_config)

            # ponytail: a worker past this check can still start a container after shutdown's kill sweep, in the narrow window before the docker call; shutdown waits for it, bounded by ticket_timeout_s.
            if self._stop.is_set():
                logger.info(f"orchestrator: ticket {tid} stopping, run skipped")
                return

            mode = "start" if classification.action in _START_ACTIONS else "resume"
            base_branch = parse_base_branch(snapshot.ticket.description, self.settings.default_base_branch)
            outcome = self._run_ticket(str(tid), tmp, mode, base_branch, self.settings)

            finished = self._publish(
                self.gateway, self.settings, snapshot, started, outcome, helper_config=self._helper_config
            )
            if finished is None:
                logger.info(f"orchestrator: ticket {tid} run not finished")
            else:
                logger.info(f"orchestrator: ticket {tid} finished {finished.result}")
        except Exception as exc:
            logger.warning(f"orchestrator: ticket {tid} worker failed: {type(exc).__name__}: {exc}")
        finally:
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)
            with self._lock:
                self._in_flight.discard(tid)

    def run_forever(self) -> None:
        """Poll and dispatch in a loop until stopped, killing in-flight runs on the way out."""
        s = self.settings
        logger.info(f"orchestrator: started, poll every {s.poll_interval_s}s, max {s.max_parallel} parallel")
        try:
            self.recover()
            while True:
                try:
                    self.poll_once()
                except Exception as exc:
                    logger.warning(f"orchestrator: poll failed: {type(exc).__name__}: {exc}")
                if self._stop.wait(s.poll_interval_s):
                    break
        except KeyboardInterrupt:
            logger.info("orchestrator: interrupted")
        finally:
            self.shutdown()

    def stop(self) -> None:
        """Ask run_forever to stop after its current poll cycle; on its own, kills nothing (shutdown does)."""
        self._stop.set()

    def shutdown(self) -> None:
        """Kill every in-flight run and wait for its worker to notice, then close the pool."""
        self._stop.set()
        logger.info("orchestrator: stopping, killing in-flight runs")
        with self._lock:
            in_flight = set(self._in_flight)
        for tid in in_flight:
            try:
                self._kill_ticket(str(tid))
            except RunnerError as exc:
                logger.warning(f"orchestrator: ticket {tid} kill failed: {type(exc).__name__}")
        self._pool.shutdown(wait=True)
