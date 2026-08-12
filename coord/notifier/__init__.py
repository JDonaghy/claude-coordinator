"""Fleet notifier (#1632) — phone push when nobody is coming.

    "Notify when the pipeline has stopped, or is stalled, and will not
    advance without a human."

Explicitly **not** "something bad happened".  A failed test, a
request-changes review, a mechanical merge conflict — the auto-loop
already handles all of those, and pushing them is the noise that trains an
operator to mute the channel.  In normal operation this fires
approximately never.

Layout, in dependency order (each layer knows nothing of the one below it):

``models``      event vocabulary, conditions ranked by confidence
``baseline``    stratified duration/silence baselines learned from history
``predicate``   pure: snapshot + baselines -> events, plus dedupe/escalation
``digest``      quiet hours as a deferral window; the 08:00 coalesced digest
``transport``   the ntfy seam — the ONLY module that knows about HTTP
``store``       durable ledger / held events / urgent drives / drive nudges
``collect``     the only module that reads the live fleet
``service``     one tick, wired together, and guaranteed not to raise

Separate from the fleet-health milestone (#53 / epic #1625) on purpose:
health checks are one *producer* of events, drive/pipeline state is
another, and both want the same phone-reachable, quiet-hours-aware
channel.
"""

from coord.notifier.models import NotifyEvent
from coord.notifier.service import TickResult, tick

__all__ = ["NotifyEvent", "TickResult", "tick"]
