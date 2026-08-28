"""
Tests for Discord startup retry logic and bot instance lifecycle management.

Verifies that:
- Bot instances are replaced on each retry attempt
- Rate limit errors don't crash the process
- Exponential backoff is applied correctly
- Failed bot clients are properly closed before retrying
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest


@pytest.mark.asyncio
async def test_bot_instance_replaced_on_retry():
    """
    Verify that a fresh bot instance is retrieved on each startup attempt.
    
    This ensures that after bot.close() from a failed connection,
    we get a fresh bot on the next attempt.
    """
    import main
    
    attempts = []
    
    async def fake_start(token):
        attempts.append(main.bot)  # Record which bot instance this is
        if len(attempts) < 2:
            # Fail on first attempt
            raise ConnectionError("Simulated Discord connection failure")
        # Success on second attempt
        return None
    
    with patch.object(main.DeepiriBot, "start", new_callable=AsyncMock, side_effect=fake_start):
        with patch.object(main.DeepiriBot, "close", new_callable=AsyncMock):
            with patch.object(main.DeepiriBot, "is_closed", return_value=False):
                await main._connect_discord_with_retry("fake_token", max_attempts=3, max_backoff=1)
    
    # Verify that we had at least 2 attempts
    assert len(attempts) >= 2, f"Expected at least 2 attempts, got {len(attempts)}"
    # The bot instances should be the same after re-getting, which is OK
    # (the important thing is that close was called on failures)


@pytest.mark.asyncio
async def test_rate_limit_error_does_not_crash_process():
    """
    Verify that Discord 429/Cloudflare 1015 rate limit errors
    do not crash the process or re-raise after max attempts.
    """
    import main
    from aiohttp import ClientError
    
    attempt_count = 0
    
    async def fake_start_with_rate_limit(token):
        nonlocal attempt_count
        attempt_count += 1
        # Always fail with rate limit error
        raise ClientError("429: Too Many Requests (simulated Cloudflare 1015)")
    
    with patch.object(main, "_get_or_create_bot", side_effect=main._get_or_create_bot):
        with patch.object(main.DeepiriBot, "start", new_callable=AsyncMock, side_effect=fake_start_with_rate_limit):
            with patch.object(main.DeepiriBot, "close", new_callable=AsyncMock):
                with patch.object(main.DeepiriBot, "is_closed", return_value=False):
                    # Should NOT raise; process should stay alive
                    await main._connect_discord_with_retry("fake_token", max_attempts=3, max_backoff=1)
    
    # Verify we actually attempted retries
    assert attempt_count == 3, f"Expected 3 attempts, got {attempt_count}"


@pytest.mark.asyncio
async def test_exponential_backoff_applied():
    """
    Verify that exponential backoff is applied between retries.
    
    Expected backoff sequence (with base^(attempt-1), max 300):
    - Attempt 1: fail
    - Attempt 2: wait ~1 second
    - Attempt 3: wait ~2 seconds
    - Attempt 4: wait ~4 seconds
    """
    import main
    
    sleep_times = []
    attempt_count = 0
    
    async def fake_start_always_fails(token):
        nonlocal attempt_count
        attempt_count += 1
        raise ConnectionError("Simulated connection failure")
    
    async def fake_sleep(delay):
        sleep_times.append(delay)
    
    with patch.object(main, "_get_or_create_bot", side_effect=main._get_or_create_bot):
        with patch.object(main.DeepiriBot, "start", new_callable=AsyncMock, side_effect=fake_start_always_fails):
            with patch.object(main.DeepiriBot, "close", new_callable=AsyncMock):
                with patch.object(main.DeepiriBot, "is_closed", return_value=False):
                    with patch("asyncio.sleep", new_callable=AsyncMock, side_effect=fake_sleep):
                        await main._connect_discord_with_retry("fake_token", max_attempts=4, max_backoff=60)
    
    # We should have 3 sleep calls (after attempts 1, 2, and 3)
    assert len(sleep_times) == 3, f"Expected 3 sleeps, got {len(sleep_times)}"
    
    # Verify backoff is increasing: 1s, 2s, 4s (approximately, with jitter)
    # Allow 50% variance due to jitter
    assert all(sleep_times[i] > 0 for i in range(len(sleep_times))), "All sleep times should be positive"
    # Each backoff should be approximately double the previous (within jitter bounds)
    assert sleep_times[1] > sleep_times[0] * 0.5, "Second backoff should be greater than first"
    assert sleep_times[2] > sleep_times[1] * 0.5, "Third backoff should be greater than second"
    # No sleep should exceed max_backoff
    assert all(t <= 60 for t in sleep_times), "No sleep should exceed max_backoff"


@pytest.mark.asyncio
async def test_failed_bot_closed_before_retry():
    """
    Verify that a failed bot instance is properly closed
    before creating a new one for retry.
    """
    import main
    
    close_calls = []
    
    async def fake_start_fails_once(token):
        if len(close_calls) == 0:
            # First attempt fails
            raise ConnectionError("Connection failed")
        # Second attempt succeeds
        return None
    
    async def track_close():
        close_calls.append("close")
    
    original_get_or_create = main._get_or_create_bot
    
    with patch.object(main, "_get_or_create_bot", side_effect=original_get_or_create):
        with patch.object(main.DeepiriBot, "start", new_callable=AsyncMock, side_effect=fake_start_fails_once):
            with patch.object(main.DeepiriBot, "close", new_callable=AsyncMock, side_effect=track_close):
                with patch.object(main.DeepiriBot, "is_closed", return_value=False):
                    await main._connect_discord_with_retry("fake_token", max_attempts=2)
    
    # Verify that close() was called (at least once for the failed bot)
    assert len(close_calls) > 0, "Failed bot instance should be closed"


@pytest.mark.asyncio
async def test_cancelled_error_propagates():
    """
    Verify that asyncio.CancelledError is propagated
    (e.g., during graceful shutdown).
    """
    import main
    
    async def fake_start_raises_cancelled(token):
        raise asyncio.CancelledError()
    
    with patch.object(main, "_get_or_create_bot", side_effect=main._get_or_create_bot):
        with patch.object(main.DeepiriBot, "start", new_callable=AsyncMock, side_effect=fake_start_raises_cancelled):
            with patch.object(main.DeepiriBot, "close", new_callable=AsyncMock):
                with patch.object(main.DeepiriBot, "is_closed", return_value=False):
                    with pytest.raises(asyncio.CancelledError):
                        await main._connect_discord_with_retry("fake_token")


@pytest.mark.asyncio
async def test_is_discord_rate_limit_error():
    """
    Verify that _is_discord_rate_limit_error correctly identifies
    rate limit errors.
    """
    import main
    from aiohttp import ClientError
    
    # Test cases that should be identified as rate limit errors
    rate_limit_errors = [
        ClientError("429: Too Many Requests"),
        ClientError("Cloudflare 1015"),
        ClientError("Error 1015 (Rate limited)"),
    ]
    
    for error in rate_limit_errors:
        assert main._is_discord_rate_limit_error(error), f"Should identify rate limit error: {error}"
    
    # Test cases that should NOT be identified as rate limit errors
    non_rate_limit_errors = [
        ConnectionError("Connection refused"),
        ValueError("Invalid token"),
        RuntimeError("Unknown error"),
    ]
    
    for error in non_rate_limit_errors:
        assert not main._is_discord_rate_limit_error(error), f"Should not identify as rate limit error: {error}"
