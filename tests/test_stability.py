from gametrans.config import StabilityConfig
from gametrans.stability import StabilityTracker


def make(frames=2, max_wait_ms=220, **kw):
    return StabilityTracker(StabilityConfig(frames_required=frames, max_wait_ms=max_wait_ms, **kw))


def test_requires_two_consistent_reads():
    tracker = make(frames=2)
    assert tracker.feed("Hello traveller", now=0.0).ready is False
    decision = tracker.feed("Hello traveller", now=0.05)
    assert decision.ready is True
    assert decision.text == "Hello traveller"


def test_eager_mode_emits_immediately():
    tracker = make(frames=1)
    assert tracker.feed("Go north", now=0.0).ready is True


def test_same_line_is_not_translated_twice():
    tracker = make(frames=1)
    assert tracker.feed("Go north", now=0.0).ready is True
    assert tracker.feed("Go north", now=0.1).ready is False
    assert tracker.feed("Go  NORTH!", now=0.2).ready is False  # same after normalising


def test_typewriter_text_waits_for_the_full_line():
    tracker = make(frames=2, max_wait_ms=5000)
    assert tracker.feed("You must", now=0.0).ready is False
    assert tracker.feed("You must go", now=0.05).ready is False
    assert tracker.feed("You must go to", now=0.10).ready is False
    decision = tracker.feed("You must go to the castle", now=0.15)
    assert decision.ready is False
    decision = tracker.feed("You must go to the castle", now=0.20)
    assert decision.ready is True
    assert decision.text == "You must go to the castle"


def test_max_wait_bounds_the_latency_cost():
    tracker = make(frames=5, max_wait_ms=100)
    assert tracker.feed("A long line here", now=0.0).ready is False
    decision = tracker.feed("A long line here", now=0.5)
    assert decision.ready is True
    assert decision.reason == "timeout"


def test_noise_is_rejected():
    tracker = make(frames=1)
    assert tracker.feed("...", now=0.0).ready is False
    assert tracker.feed("", now=0.1).ready is False


def test_replacement_line_restarts_the_count():
    tracker = make(frames=2)
    tracker.feed("First line", now=0.0)
    assert tracker.feed("Completely different", now=0.05).ready is False
    assert tracker.feed("Completely different", now=0.10).ready is True


def test_clear_current_allows_the_same_line_again():
    tracker = make(frames=1)
    assert tracker.feed("Repeat me", now=0.0).ready is True
    assert tracker.feed("Repeat me", now=0.1).ready is False
    tracker.reset()
    assert tracker.feed("Repeat me", now=0.2).ready is True
