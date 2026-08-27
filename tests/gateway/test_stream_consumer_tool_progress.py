"""Tests for tool-progress-in-native-stream (single bubble) feature.

Validates that tool-progress lines are injected into the native streaming
bubble and properly overwritten by text deltas (Strategy B).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.stream_consumer import (
    GatewayStreamConsumer,
    StreamConsumerConfig,
    _TOOL_PROGRESS,
)


def _make_native_streaming_adapter(*, supports_native: bool = True):
    """Build a BasePlatformAdapter subclass that supports native streaming."""
    from gateway.platforms.base import BasePlatformAdapter

    NativeStreamingAdapter = type(
        "NativeStreamingAdapter",
        (BasePlatformAdapter,),
        {
            "MAX_MESSAGE_LENGTH": 4096,
            "SUPPORTS_MESSAGE_EDITING": False,
            "SUPPORTS_NATIVE_STREAMING": True,
        },
    )
    NativeStreamingAdapter.__abstractmethods__ = frozenset()
    adapter = NativeStreamingAdapter.__new__(NativeStreamingAdapter)
    adapter._typing_paused = set()
    adapter._fatal_error_message = None
    adapter.frames = []

    def _supports(chat_type=None, metadata=None):
        return bool(supports_native)
    adapter.supports_native_streaming = _supports

    async def _send_stream_frame(
        text, *, finalize=False, chat_id=None, reply_to=None, **kwargs
    ):
        adapter.frames.append({
            "text": text,
            "finalize": finalize,
            "chat_id": chat_id,
        })
        return True
    adapter.send_stream_frame = _send_stream_frame

    adapter.send = AsyncMock(
        return_value=SimpleNamespace(success=True, message_id="fallback_msg"),
    )
    adapter.edit_message = AsyncMock(
        return_value=SimpleNamespace(success=True),
    )
    return adapter


def _make_consumer(*, native_streaming: bool = True) -> GatewayStreamConsumer:
    """Create a GatewayStreamConsumer configured for native streaming."""
    adapter = _make_native_streaming_adapter(supports_native=native_streaming)
    cfg = StreamConsumerConfig(chat_type="dm", cursor="▌")
    consumer = GatewayStreamConsumer(adapter, "chat-1", cfg)
    # Force native streaming resolution
    consumer._use_native_streaming = native_streaming
    return consumer


# === UNIT TESTS ===


class TestAcceptsToolProgress:
    """Tests for the accepts_tool_progress property."""

    def test_native_streaming_accepts(self):
        consumer = _make_consumer(native_streaming=True)
        assert consumer.accepts_tool_progress is True

    def test_non_native_does_not_accept(self):
        consumer = _make_consumer(native_streaming=False)
        assert consumer.accepts_tool_progress is False


class TestOnToolProgress:
    """Tests for on_tool_progress() enqueue behavior."""

    def test_enqueues_sentinel(self):
        consumer = _make_consumer()
        consumer.on_tool_progress("🔍 Searching...")
        item = consumer._queue.get_nowait()
        assert isinstance(item, tuple)
        assert len(item) == 2
        assert item[0] is _TOOL_PROGRESS
        assert item[1] == "🔍 Searching..."

    def test_empty_line_not_enqueued(self):
        consumer = _make_consumer()
        consumer.on_tool_progress("")
        assert consumer._queue.empty()


class TestComposeFrameContent:
    """Tests for _compose_frame_content() composition logic (Strategy B)."""

    def test_only_tool_lines(self):
        consumer = _make_consumer()
        consumer._tool_progress_lines = ["🔍 Searching...", "💻 Running git log"]
        result = consumer._compose_frame_content()
        assert result == "🔍 Searching...\n💻 Running git log"

    def test_only_accumulated(self):
        consumer = _make_consumer()
        consumer._accumulated = "Here is the answer."
        result = consumer._compose_frame_content()
        assert result == "Here is the answer."

    def test_both_accumulated_and_tool_lines_strategy_b(self):
        """Strategy B: text + separator + tool status at bottom."""
        consumer = _make_consumer()
        consumer._accumulated = "Here is some text so far."
        consumer._tool_progress_lines = ["🔍 Searching the web..."]
        result = consumer._compose_frame_content()
        assert result == "Here is some text so far.\n\n---\n🔍 Searching the web..."

    def test_multiple_tool_lines_stacked(self):
        consumer = _make_consumer()
        consumer._tool_progress_lines = [
            "🔍 web_search: 'python'",
            "💻 terminal: git log",
            "📄 read_file: main.py",
        ]
        result = consumer._compose_frame_content()
        assert "web_search" in result
        assert "terminal" in result
        assert "read_file" in result
        # Lines are joined with newlines
        assert result.count("\n") == 2

    def test_empty_state(self):
        consumer = _make_consumer()
        result = consumer._compose_frame_content()
        assert result == ""


class TestSegmentReset:
    """Test that segment reset clears tool progress state."""

    def test_reset_clears_tool_progress(self):
        consumer = _make_consumer()
        consumer._tool_progress_lines = ["🔍 Searching..."]
        consumer._tool_progress_active = True
        consumer._reset_segment_state()
        assert consumer._tool_progress_lines == []
        assert consumer._tool_progress_active is False


# === INTEGRATION TESTS (drain loop) ===


class TestToolProgressDrainLoop:
    """Integration tests for the drain loop + frame delivery."""

    @pytest.mark.asyncio
    async def test_tool_progress_only_then_done(self):
        """Pure tool-progress turn (no text): tool lines visible as mid-frame,
        finalize uses placeholder since no accumulated text."""
        consumer = _make_consumer()
        consumer.on_tool_progress("🔍 Searching...")
        consumer.on_tool_progress("💻 terminal: ls")

        # Start consumer so it drains tool progress and sends mid-frames
        task = asyncio.create_task(consumer.run())
        await asyncio.sleep(0.3)

        # Now finish — tool lines were already displayed as a frame
        consumer.finish()
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        frames = consumer.adapter.frames
        # Should have: seed frame, at least one mid-frame with tool lines,
        # and a finalize frame (✅ placeholder since no text)
        assert len(frames) >= 2
        # Find mid-frames that contain tool progress
        non_finalize = [f for f in frames if not f["finalize"] and f["text"]]
        assert any("Searching" in f["text"] or "terminal" in f["text"] for f in non_finalize), (
            f"Expected tool progress in mid-frames, got: {[f['text'] for f in frames]}"
        )

    @pytest.mark.asyncio
    async def test_tool_progress_then_text_clears_overlay(self):
        """Tool progress → text delta should clear tool lines from frame."""
        consumer = _make_consumer()
        consumer.on_tool_progress("🔍 Searching...")
        consumer.on_delta("Hello world")
        consumer.finish()

        task = asyncio.create_task(consumer.run())
        await asyncio.sleep(0.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # After text arrives, tool_progress_lines should be cleared
        assert consumer._tool_progress_lines == []
        assert "Hello world" in consumer._accumulated

        # The finalize frame should contain just the text
        frames = consumer.adapter.frames
        finalize_frames = [f for f in frames if f["finalize"]]
        if finalize_frames:
            assert "Hello world" in finalize_frames[-1]["text"]
            assert "Searching" not in finalize_frames[-1]["text"]

    @pytest.mark.asyncio
    async def test_text_then_tool_then_text_strategy_b(self):
        """Strategy B: text → tool → text appends tool at bottom then clears."""
        consumer = _make_consumer()

        # Phase 1: initial text
        consumer.on_delta("First part. ")
        # Phase 2: tool progress mid-stream
        consumer.on_tool_progress("🔍 web_search...")
        # Phase 3: more text arrives
        consumer.on_delta("Second part.")
        consumer.finish()

        task = asyncio.create_task(consumer.run())
        await asyncio.sleep(0.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Final state: tool lines cleared, accumulated has both text parts
        assert consumer._tool_progress_lines == []
        assert "First part." in consumer._accumulated
        assert "Second part." in consumer._accumulated

    @pytest.mark.asyncio
    async def test_parallel_tool_calls_stacked(self):
        """Multiple tool.started back-to-back should stack in overlay."""
        consumer = _make_consumer()
        consumer.on_tool_progress("🔍 web_search")
        consumer.on_tool_progress("💻 terminal")
        consumer.on_tool_progress("📄 read_file")

        # Let drain loop process and send mid-frame before finish
        task = asyncio.create_task(consumer.run())
        await asyncio.sleep(0.3)

        consumer.finish()
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # All three should have been accumulated and sent in one frame
        frames = consumer.adapter.frames
        # Find a frame that contains all three tools
        all_three = [
            f for f in frames
            if "web_search" in f["text"]
            and "terminal" in f["text"]
            and "read_file" in f["text"]
        ]
        assert len(all_three) >= 1, (
            f"Expected a frame with all 3 tools, got: {[f['text'] for f in frames]}"
        )

    @pytest.mark.asyncio
    async def test_finalize_frame_is_pure_text(self):
        """The finalize frame must only contain accumulated text, no tool lines."""
        consumer = _make_consumer()
        consumer.on_tool_progress("🔍 Searching...")
        consumer.on_delta("The answer is 42.")
        # Add a tool progress AFTER text (Strategy B scenario)
        consumer.on_tool_progress("💻 terminal: verify")
        # Then more text clears it
        consumer.on_delta(" Verified.")
        consumer.finish()

        task = asyncio.create_task(consumer.run())
        await asyncio.sleep(0.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        frames = consumer.adapter.frames
        finalize_frames = [f for f in frames if f["finalize"]]
        if finalize_frames:
            final_text = finalize_frames[-1]["text"]
            assert "The answer is 42. Verified." in final_text
            assert "Searching" not in final_text
            assert "terminal" not in final_text
            assert "---" not in final_text


# === TOOL-TIMER ANIMATION TESTS ===

import time

from gateway.stream_consumer import (
    _TIMER_TICK,
    _SPINNER_CHARS,
    _parse_tool_name,
)


class TestParseToolName:
    """Tests for the _parse_tool_name helper."""

    def test_running_terminal(self):
        assert _parse_tool_name("🔧 Running terminal...") == "terminal"

    def test_calling_web_search(self):
        assert _parse_tool_name("⚙️ Calling web_search...") == "web_search"

    def test_using_read_file(self):
        assert _parse_tool_name("📄 Using read_file...") == "read_file"

    def test_bare_tool_name(self):
        assert _parse_tool_name("🔍 Searching...") == "Searching"

    def test_no_emoji(self):
        assert _parse_tool_name("Running terminal...") == "terminal"

    def test_fallback_on_empty(self):
        assert _parse_tool_name("!!!") == "tool"


class TestToolTimerStart:
    """Tests for timer start behavior."""

    def test_timer_starts_on_first_tool_progress(self):
        """Timer handle should be armed after on_tool_progress on native streaming."""
        consumer = _make_consumer(native_streaming=True)
        # Simulate the event loop being captured (normally done in run())
        loop = asyncio.new_event_loop()
        consumer._tool_timer_loop = loop
        try:
            consumer.on_tool_progress("🔧 Running terminal...")
            # Timer handle should have been armed
            assert consumer._tool_timer_handle is not None
            assert "terminal" in consumer._tool_start_times
        finally:
            consumer._tool_timer_handle.cancel()
            loop.close()

    def test_timer_does_not_start_on_non_native(self):
        """Timer should NOT arm when native streaming is off."""
        consumer = _make_consumer(native_streaming=False)
        loop = asyncio.new_event_loop()
        consumer._tool_timer_loop = loop
        try:
            consumer.on_tool_progress("🔧 Running terminal...")
            assert consumer._tool_timer_handle is None
            assert consumer._tool_start_times == {}
        finally:
            loop.close()

    def test_multiple_tools_get_independent_start_times(self):
        """Each tool should have its own start time."""
        consumer = _make_consumer(native_streaming=True)
        loop = asyncio.new_event_loop()
        consumer._tool_timer_loop = loop
        try:
            consumer.on_tool_progress("🔧 Running terminal...")
            consumer.on_tool_progress("⚙️ Calling web_search...")
            assert "terminal" in consumer._tool_start_times
            assert "web_search" in consumer._tool_start_times
            assert len(consumer._tool_start_times) == 2
        finally:
            consumer._tool_timer_handle.cancel()
            loop.close()


class TestToolTimerTick:
    """Tests for the tick callback behavior."""

    def test_tick_updates_progress_lines(self):
        """A manual tick should rebuild _tool_progress_lines with spinner + elapsed."""
        consumer = _make_consumer(native_streaming=True)
        loop = asyncio.new_event_loop()
        consumer._tool_timer_loop = loop
        try:
            # Manually set state as if on_tool_progress had been called
            consumer._tool_start_times = {"terminal": time.monotonic() - 5}
            consumer._tool_timer_tick_count = 0

            # Invoke tick directly
            consumer._tool_timer_tick()

            assert len(consumer._tool_progress_lines) == 1
            line = consumer._tool_progress_lines[0]
            # Should contain spinner char, tool name, and elapsed time
            assert "terminal" in line
            assert "s)" in line
            # Spinner should be the second char (tick_count becomes 1)
            assert line[0] == _SPINNER_CHARS[1]
            # Should have put _TIMER_TICK in queue
            item = consumer._queue.get_nowait()
            assert item is _TIMER_TICK
            # Should have set _tool_progress_active
            assert consumer._tool_progress_active is True
            # Timer should be re-armed
            assert consumer._tool_timer_handle is not None
        finally:
            if consumer._tool_timer_handle:
                consumer._tool_timer_handle.cancel()
            loop.close()

    def test_tick_multiple_tools(self):
        """Tick with multiple tools produces one line per tool."""
        consumer = _make_consumer(native_streaming=True)
        loop = asyncio.new_event_loop()
        consumer._tool_timer_loop = loop
        try:
            now = time.monotonic()
            consumer._tool_start_times = {
                "terminal": now - 10,
                "web_search": now - 3,
            }
            consumer._tool_timer_tick_count = 0

            consumer._tool_timer_tick()

            assert len(consumer._tool_progress_lines) == 2
            names_in_lines = " ".join(consumer._tool_progress_lines)
            assert "terminal" in names_in_lines
            assert "web_search" in names_in_lines
        finally:
            if consumer._tool_timer_handle:
                consumer._tool_timer_handle.cancel()
            loop.close()

    def test_tick_no_rearm_when_tools_cleared(self):
        """Tick with no active tools should not re-arm the timer."""
        consumer = _make_consumer(native_streaming=True)
        loop = asyncio.new_event_loop()
        consumer._tool_timer_loop = loop
        try:
            consumer._tool_start_times = {}  # no tools
            consumer._tool_timer_tick()
            assert consumer._tool_timer_handle is None
        finally:
            loop.close()


class TestToolTimerStop:
    """Tests for timer stop conditions."""

    def test_timer_stops_on_text_delta(self):
        """_append_accumulated should stop the timer."""
        consumer = _make_consumer(native_streaming=True)
        loop = asyncio.new_event_loop()
        consumer._tool_timer_loop = loop
        try:
            consumer._tool_start_times = {"terminal": time.monotonic()}
            consumer._tool_timer_handle = loop.call_later(100, lambda: None)

            consumer._append_accumulated("Hello")

            assert consumer._tool_timer_handle is None
            assert consumer._tool_start_times == {}
        finally:
            loop.close()

    def test_timer_stops_on_finalize(self):
        """_stop_tool_timer called on got_done path."""
        consumer = _make_consumer(native_streaming=True)
        loop = asyncio.new_event_loop()
        consumer._tool_timer_loop = loop
        try:
            consumer._tool_start_times = {"terminal": time.monotonic()}
            consumer._tool_timer_handle = loop.call_later(100, lambda: None)

            consumer._stop_tool_timer()

            assert consumer._tool_timer_handle is None
            assert consumer._tool_start_times == {}
            assert consumer._tool_timer_tick_count == 0
        finally:
            loop.close()

    def test_timer_stops_on_segment_reset(self):
        """_reset_segment_state should stop the timer."""
        consumer = _make_consumer(native_streaming=True)
        loop = asyncio.new_event_loop()
        consumer._tool_timer_loop = loop
        try:
            consumer._tool_start_times = {"terminal": time.monotonic()}
            consumer._tool_timer_handle = loop.call_later(100, lambda: None)

            consumer._reset_segment_state()

            assert consumer._tool_timer_handle is None
            assert consumer._tool_start_times == {}
        finally:
            loop.close()


class TestToolTimerDrainLoop:
    """Integration tests for timer ticks in the drain loop."""

    @pytest.mark.asyncio
    async def test_timer_tick_wakes_drain_loop(self):
        """_TIMER_TICK sentinel should be drained without error."""
        consumer = _make_consumer(native_streaming=True)
        # Simulate: tool_progress + manual tick + finish
        consumer.on_tool_progress("🔧 Running terminal...")
        # Put a timer tick into the queue (simulating what _tool_timer_tick does)
        consumer._queue.put(_TIMER_TICK)
        consumer.finish()

        task = asyncio.create_task(consumer.run())
        await asyncio.sleep(0.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Should not crash — the drain loop handles _TIMER_TICK gracefully
        frames = consumer.adapter.frames
        assert len(frames) >= 1  # At least a seed frame

    @pytest.mark.asyncio
    async def test_timer_produces_animated_frames(self):
        """With real timer ticking, frames should update with elapsed time."""
        consumer = _make_consumer(native_streaming=True)

        # Start the run loop
        task = asyncio.create_task(consumer.run())
        await asyncio.sleep(0.1)  # Let run() start and set _tool_timer_loop

        # Inject tool progress (this arms the timer via on_tool_progress)
        consumer.on_tool_progress("🔧 Running terminal...")
        # Wait >2 seconds for at least 2 ticks
        await asyncio.sleep(2.5)

        # Finish up
        consumer.finish()
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        frames = consumer.adapter.frames
        # Filter non-finalize frames that contain terminal timer output
        timer_frames = [
            f for f in frames
            if not f["finalize"] and f["text"] and "terminal" in f["text"] and "s)" in f["text"]
        ]
        # We should have at least 2 frames showing different elapsed times
        assert len(timer_frames) >= 2, (
            f"Expected >=2 timer frames, got {len(timer_frames)}: "
            f"{[f['text'] for f in timer_frames]}"
        )

    @pytest.mark.asyncio
    async def test_timer_stops_when_text_arrives(self):
        """Timer frames should stop once a text delta arrives."""
        consumer = _make_consumer(native_streaming=True)

        task = asyncio.create_task(consumer.run())
        await asyncio.sleep(0.1)

        consumer.on_tool_progress("🔧 Running terminal...")
        await asyncio.sleep(1.5)  # Let timer tick once

        # Text delta arrives → timer should stop
        consumer.on_delta("Result: done")
        await asyncio.sleep(0.3)

        # Timer should be stopped
        assert consumer._tool_start_times == {}
        assert consumer._tool_timer_handle is None

        consumer.finish()
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # The finalize frame should be pure text (no spinner)
        frames = consumer.adapter.frames
        finalize_frames = [f for f in frames if f["finalize"]]
        if finalize_frames:
            assert "Result: done" in finalize_frames[-1]["text"]
            assert "s)" not in finalize_frames[-1]["text"]


import time
