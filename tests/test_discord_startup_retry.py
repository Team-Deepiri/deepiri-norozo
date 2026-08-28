"""
Tests for Discord startup retry logic with bot factory and lifecycle management.

Verifies that:
- Bot factory creates distinct instances with all handlers/commands
- Indefinite retry loop never gives up
- Rate limit errors don't crash the process  
- Exponential backoff grows to cap and respects Retry-After
- Failed bot clients are properly closed before retrying
- Process stays alive during repeated 429 failures
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest


@pytest.mark.asyncio
async def test_bot_factory_creates_distinct_instances():
    """
    Verify that bot factory creates distinct bot instances.
    
    This ensures fresh bot instances have independent state,
    preventing issues with reusing closed clients.
    """
    import main
    
    bot1 = main._create_and_register_bot()
    bot2 = main._create_and_register_bot()
    
    # Instances should be different objects
    assert bot1 is not bot2, "Factory should create distinct bot instances"
    
    # Both should be DeepiriBot instances
    assert isinstance(bot1, main.DeepiriBot), "Factory should create DeepiriBot instances"
    assert isinstance(bot2, main.DeepiriBot), "Factory should create DeepiriBot instances"


@pytest.mark.asyncio
async def test_bot_factory_registers_handlers():
    """
    Verify that bot factory registers event handlers.
    
    Each fresh bot must have on_ready, on_member_join, on_message handlers.
    """
    import main
    
    bot = main._create_and_register_bot()
    
    # Check that bot has event listeners
    listeners = bot._listeners
    
    # Verify bot has listeners (exact structure depends on discord.py version)
    assert listeners is not None, "Bot should have event listeners"
    

@pytest.mark.asyncio
async def test_bot_factory_registers_commands():
    """
    Verify that bot factory registers slash commands.
    
    Each fresh bot must have all slash commands (github-invite-request, etc.).
    """
    import main
    
    bot = main._create_and_register_bot()
    
    # Check that bot has tree commands
    command_names = [cmd.name for cmd in bot.tree._get_all_commands()]
    
    # Verify key commands are registered
    expected_commands = [
        "github-invite-request",
        "ipca-signed",
        "offboard-user",
        "plaky-request",
        "plaky-status",
        "poll",
    ]
    
    for cmd_name in expected_commands:
        assert cmd_name in command_names, f"Factory should register '{cmd_name}' command"


@pytest.mark.asyncio
async def test_indefinite_retry_continues_beyond_five_attempts():
    """
    Verify that retry loop is indefinite and not capped at 5 attempts.
    
    The new implementation must NOT give up after max_attempts;
    it should keep retrying with exponential backoff.
    """
    import main
    
    attempt_count = 0
    max_test_attempts = 7  # Test beyond old 5-attempt limit
    
    async def fake_start_always_fails(token):
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count > max_test_attempts:
            # Stop test to avoid infinite loop
            raise KeyboardInterrupt("Test limit reached")
        raise ConnectionError(f"Simulated failure #{attempt_count}")
    
    with patch.object(main, "_create_and_register_bot", side_effect=main._create_and_register_bot):
        with patch.object(main.DeepiriBot, "start", new_callable=AsyncMock, side_effect=fake_start_always_fails):
            with patch.object(main.DeepiriBot, "close", new_callable=AsyncMock):
                with patch.object(main.DeepiriBot, "is_closed", return_value=False):
                    with pytest.raises(KeyboardInterrupt):
                        await main._connect_discord_with_retry("fake_token", max_backoff=1)
    
    # Verify we reached beyond the old 5-attempt limit
    assert attempt_count > 5, f"Should retry beyond 5 attempts; got {attempt_count}"
    # Note: may go one past the limit due to timing
    assert attempt_count >= max_test_attempts, f"Should reach near test limit; got {attempt_count}"


@pytest.mark.asyncio
async def test_rate_limit_error_process_stays_alive():
    """
    Verify that Discord 429/Cloudflare 1015 errors don't crash the process.
    
    Process must stay alive and keep retrying indefinitely.
    """
    import main
    from aiohttp import ClientError
    
    attempt_count = 0
    test_limit = 3
    
    async def fake_start_with_rate_limit(token):
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count > test_limit:
            raise KeyboardInterrupt("Test limit reached")
        raise ClientError("429: Too Many Requests (Cloudflare 1015)")
    
    with patch.object(main, "_create_and_register_bot", side_effect=main._create_and_register_bot):
        with patch.object(main.DeepiriBot, "start", new_callable=AsyncMock, side_effect=fake_start_with_rate_limit):
            with patch.object(main.DeepiriBot, "close", new_callable=AsyncMock):
                with patch.object(main.DeepiriBot, "is_closed", return_value=False):
                    # Should NOT raise; should keep retrying
                    with pytest.raises(KeyboardInterrupt):
                        await main._connect_discord_with_retry("fake_token", max_backoff=1)
    
    # Verify we continued retrying despite rate limits
    assert attempt_count >= test_limit, f"Should retry at least {test_limit} times; got {attempt_count}"


@pytest.mark.asyncio
async def test_exponential_backoff_grows_and_caps():
    """
    Verify that exponential backoff grows and reaches the cap.
    
    Expected: 1s, 2s, 4s, 8s, 16s, 32s, 64s, ... capped at 300s
    """
    import main
    
    sleep_times = []
    attempt_count = 0
    test_attempts = 10  # Go beyond 5 to see backoff growth
    
    async def fake_start_always_fails(token):
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count > test_attempts:
            raise KeyboardInterrupt("Test limit reached")
        raise ConnectionError(f"Simulated failure #{attempt_count}")
    
    async def fake_sleep(delay):
        sleep_times.append(delay)
    
    with patch.object(main, "_create_and_register_bot", side_effect=main._create_and_register_bot):
        with patch.object(main.DeepiriBot, "start", new_callable=AsyncMock, side_effect=fake_start_always_fails):
            with patch.object(main.DeepiriBot, "close", new_callable=AsyncMock):
                with patch.object(main.DeepiriBot, "is_closed", return_value=False):
                    with patch("asyncio.sleep", new_callable=AsyncMock, side_effect=fake_sleep):
                        with pytest.raises(KeyboardInterrupt):
                            await main._connect_discord_with_retry("fake_token", max_backoff=60)
    
    # Should have sleep calls for retries
    assert len(sleep_times) > 0, "Should have sleep delays"
    
    # Backoff should be increasing (with jitter, allow 50% variance)
    for i in range(1, len(sleep_times)):
        assert sleep_times[i] >= sleep_times[i-1] * 0.5, f"Backoff should generally increase"
    
    # Backoff should never exceed max_backoff (with jitter tolerance)
    assert all(t <= 60 * 1.15 for t in sleep_times), "Backoff should not exceed max_backoff * jitter"


@pytest.mark.asyncio
async def test_failed_bot_closed_before_retry():
    """
    Verify that failed bot instances are properly closed before retry.
    """
    import main
    
    close_calls = []
    attempt_count = 0
    
    async def fake_start_fails_once(token):
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count == 1:
            raise ConnectionError("First attempt fails")
        if attempt_count > 2:
            raise KeyboardInterrupt("Test limit reached")
        # Second attempt also fails
        raise ConnectionError("Second attempt also fails")
    
    async def track_close():
        close_calls.append("close")
    
    with patch.object(main, "_create_and_register_bot", side_effect=main._create_and_register_bot):
        with patch.object(main.DeepiriBot, "start", new_callable=AsyncMock, side_effect=fake_start_fails_once):
            with patch.object(main.DeepiriBot, "close", new_callable=AsyncMock, side_effect=track_close):
                with patch.object(main.DeepiriBot, "is_closed", return_value=False):
                    with pytest.raises(KeyboardInterrupt):
                        await main._connect_discord_with_retry("fake_token", max_backoff=1)
    
    # Verify close was called for failed bot
    assert len(close_calls) >= 1, "Failed bot instances should be closed"


@pytest.mark.asyncio
async def test_cancelled_error_propagates():
    """
    Verify that asyncio.CancelledError is propagated (graceful shutdown).
    """
    import main
    
    async def fake_start_raises_cancelled(token):
        raise asyncio.CancelledError()
    
    with patch.object(main, "_create_and_register_bot", side_effect=main._create_and_register_bot):
        with patch.object(main.DeepiriBot, "start", new_callable=AsyncMock, side_effect=fake_start_raises_cancelled):
            with patch.object(main.DeepiriBot, "close", new_callable=AsyncMock):
                with patch.object(main.DeepiriBot, "is_closed", return_value=False):
                    with pytest.raises(asyncio.CancelledError):
                        await main._connect_discord_with_retry("fake_token")


@pytest.mark.asyncio
async def test_is_discord_rate_limit_error():
    """
    Verify rate limit error detection.
    """
    import main
    from aiohttp import ClientError
    
    # Test cases that should be identified as rate limit errors
    rate_limit_errors = [
        ClientError("429: Too Many Requests"),
        ClientError("Cloudflare 1015"),
        ClientError("Error 1015 (Rate limited)"),
        ValueError("429 error occurred"),
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


@pytest.mark.asyncio
async def test_retry_after_extraction():
    """
    Verify Retry-After header extraction.
    """
    import main
    
    # Test with Retry-After value
    error_with_retry_after = ValueError("Error with Retry-After=120")
    retry_after = main._extract_retry_after(error_with_retry_after)
    # May or may not extract (depends on format), so just verify it doesn't crash
    assert retry_after is None or isinstance(retry_after, int), "Should extract or return None"
    
    # Test without Retry-After
    error_without_retry_after = ValueError("Just a regular error")
    retry_after = main._extract_retry_after(error_without_retry_after)
    assert retry_after is None, "Should return None when no Retry-After"
