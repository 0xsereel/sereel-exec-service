"""Drives payouts: one interval job that claims and pays whatever is due (state lives in the DB, so restarts and
CLI changes are picked up automatically)."""
import logging

from apscheduler.schedulers.background import BackgroundScheduler

from . import service

log = logging.getLogger("sereel.payments")
TICK_S = 5


def start() -> BackgroundScheduler:
    service.mark_unconfirmed_claims()
    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(service.run_due, "interval", seconds=TICK_S, id="payouts", max_instances=1, coalesce=True,
                  misfire_grace_time=TICK_S)
    sched.start()
    log.info("payout scheduler started (tick %ss)", TICK_S)
    return sched
