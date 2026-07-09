#!/usr/bin/env python3
"""
Shared Matrix client for delegation communication.

Provides both sync (urllib) and async (aiohttp) methods for Matrix API access.
Used by both delegation_core.py (CLI commands) and delegation_worker.py (async daemon).
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

try:
    from . import path_utils
except ImportError:
    import os as _os, sys as _sys
    _src = _os.path.dirname(_os.path.abspath(__file__))
    if _src not in _sys.path:
        _sys.path.insert(0, _src)
    import path_utils

__all__ = ["MatrixClient"]


class MatrixClient:
    """Unified Matrix client with sync and async methods.

    Sync methods (urllib) — for CLI commands in delegation_core.py.
    Async methods (aiohttp) — for the daemon in delegation_worker.py.
    """

    def __init__(self, config: dict):
        self.config = config
        # Strip trailing slash from URL to avoid double-slash in API paths
        self.url = config['matrix']['url'].rstrip('/')
        self.user_id = config['matrix']['user_id']
        self.access_token = config['matrix']['access_token']
        self.logger = logging.getLogger('matrix_client')

    # ------------------------------------------------------------------ #
    # Shared utilities
    # ------------------------------------------------------------------ #

    @staticmethod
    def setup_command_logging(config_path: Path) -> logging.Logger:
        """Setup logging to file for command-line scripts."""
        config_dir = config_path.parent
        log_file = config_dir / "delegator-delegatee-skill.log"

        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
            handlers=[
                logging.FileHandler(log_file),
                logging.StreamHandler(sys.stdout)
            ]
        )
        return logging.getLogger('delegation_skill')

    def _encode_room_id(self, room_id: str) -> str:
        """Encode room ID for use in URL paths."""
        return quote(room_id, safe='')

    @staticmethod
    def _sanitize_url(url: str) -> str:
        """Remove access tokens and pagination tokens from URLs before logging."""
        import re
        url = re.sub(r'access_token=[^&\s]+', 'access_token=REDACTED', url)
        url = re.sub(r'from=[^&\s]+', 'from=REDACTED', url)
        url = re.sub(r'Bearer\s+[\w\-\.]+', 'Bearer REDACTED', url)
        return url

    # ------------------------------------------------------------------ #
    # Sync methods (urllib) — for CLI commands
    # ------------------------------------------------------------------ #

    def send_message_sync(self, room_id: str, message: str) -> bool:
        """Send a text message to a Matrix room (synchronous).

        If the message contains Markdown that renders to non-trivial HTML
        (more than a single <p>), include `format` + `formatted_body` so
        Element / web clients render headings, lists, code blocks, tables,
        links etc. instead of printing the raw Markdown source.

        Returns True on success, False on failure.
        """
        import urllib.request
        import urllib.error

        content = {"msgtype": "m.text", "body": message}
        try:
            import markdown as _md
            html = _md.markdown(message, extensions=["fenced_code", "tables"])
            if html.strip() and html.strip() != f"<p>{message}</p>":
                content["format"] = "org.matrix.custom.html"
                content["formatted_body"] = html
        except Exception:
            pass
        body = json.dumps(content)

        encoded_room_id = self._encode_room_id(room_id)
        url = f"{self.url}/_matrix/client/r0/rooms/{encoded_room_id}/send/m.room.message"

        req = urllib.request.Request(
            url,
            data=body.encode('utf-8'),
            headers={
                'Authorization': f'Bearer {self.access_token}',
                'Content-Type': 'application/json'
            },
            method='POST'
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status == 200
        except urllib.error.HTTPError as e:
            self.logger.error(f"Matrix API error: {e.code} — {self._sanitize_url(e.url)}")
            return False
        except Exception as e:
            self.logger.error(f"Failed to send message: {self._sanitize_url(str(e))}")
            return False

    # ------------------------------------------------------------------ #
    # Async methods (aiohttp) — for daemon polling
    # ------------------------------------------------------------------ #

    async def send_message(self, room_id: str, message: str) -> Dict[str, Any]:
        """Send a text message to a Matrix room (async).

        Retries indefinitely on transient errors (5xx, timeouts, connection
        errors, rate-limits) with backoff capped at 60 s.  Each retry is
        logged so we can see the actual reason a delivery is delayed.

        The delegation contract assumes posts succeed eventually — dropping
        a handoff or progress report leaves orch stuck waiting forever.
        This method keeps trying until the Matrix homeserver accepts it.
        """
        import aiohttp

        body = {
            "msgtype": "m.text",
            "body": message
        }
        try:
            import markdown as _md
            html = _md.markdown(message, extensions=["fenced_code", "tables"])
            if html.strip() and html.strip() != f"<p>{message}</p>":
                body["format"] = "org.matrix.custom.html"
                body["formatted_body"] = html
        except Exception:
            pass

        encoded_room_id = self._encode_room_id(room_id)
        url = f"{self.url}/_matrix/client/r0/rooms/{encoded_room_id}/send/m.room.message"
        headers = {"Authorization": f"Bearer {self.access_token}"}

        async def _do_send():
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, json=body) as resp:
                    if resp.status == 429:
                        retry_after = int(resp.headers.get('Retry-After', 10))
                        raise asyncio.TimeoutError(f"Rate limited, wait {retry_after}s")
                    if resp.status >= 500:
                        raise aiohttp.ClientError(f"Server error: {resp.status}")
                    if resp.status != 200:
                        # 4xx (other than 429) is a permanent client error
                        # — propagate without retry so we don't loop forever
                        # on a malformed payload.
                        raise Exception(f"Matrix API permanent error: {resp.status}")
                    result = await resp.json()
                    return result

        # max_retries=1_000_000 is "effectively forever" — about 700 days at
        # the capped 60 s backoff, well beyond any operational window.  A
        # 4xx response (raised as a bare Exception above) skips the retry
        # path and propagates immediately.
        try:
            return await _retry_async(
                _do_send,
                tags=f"send_message({room_id})",
                logger=self.logger,
                max_retries=1_000_000,
                max_delay=60,
            )
        except Exception as e:
            self.logger.error(
                f"send_message({room_id}): permanent failure: "
                f"{self._sanitize_url(str(e))}",
                exc_info=True,
            )
            raise

    async def get_messages(
        self,
        room_id: str,
        from_token: Optional[str] = None,
        limit: int = 50,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Fetch recent messages from a room with retry on transient errors.

        from_token should be a pagination token from a previous /messages call,
        NOT an event_id. Event_ids cannot be used as pagination tokens.

        Returns:
            Tuple of (chunk, end_token) where end_token is the batch token for
            next pagination. end_token is None if no more messages.
        """
        import aiohttp

        encoded_room_id = self._encode_room_id(room_id)
        base_url = f"{self.url}/_matrix/client/r0/rooms/{encoded_room_id}/messages"

        # Build query string manually
        params = ["dir=b", f"limit={limit}"]
        if from_token and not from_token.startswith('$'):
            params.append(f"from={quote(from_token, safe='')}" )
        query_string = "&".join(params)
        full_url = f"{base_url}?{query_string}"

        headers = {"Authorization": f"Bearer {self.access_token}"}

        async def _do_get():
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(full_url, headers=headers) as resp:
                    if resp.status == 429:
                        retry_after = int(resp.headers.get('Retry-After', 10))
                        raise asyncio.TimeoutError(f"Rate limited, wait {retry_after}s")
                    if resp.status >= 500:
                        raise aiohttp.ClientError(f"Server error: {resp.status}")
                    if resp.status != 200:
                        return ([], None)
                    result = await resp.json()
                    chunk = result.get('chunk', [])
                    end_token = result.get('end')
                    return (chunk, end_token)

        try:
            return await _retry_async(_do_get, tags=f"get_messages({room_id})", logger=self.logger)
        except Exception as e:
            self.logger.error(f"Failed to fetch messages after retries: {self._sanitize_url(str(e))}")
            raise

    # ------------------------------------------------------------------
    # Async methods (aiohttp) — for daemon polling
    # ------------------------------------------------------------------ #

    async def get_room_state(self, room_id: str) -> Dict[str, Any]:
        """Get the current state of a Matrix room (async).

        Returns the room state dict.
        Raises Exception on failure.
        """
        import aiohttp

        encoded_room_id = self._encode_room_id(room_id)
        url = f"{self.url}/_matrix/client/r0/rooms/{encoded_room_id}/state"
        headers = {"Authorization": f"Bearer {self.access_token}"}

        async def _do_get():
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 429:
                        retry_after = int(resp.headers.get('Retry-After', 10))
                        raise asyncio.TimeoutError(f"Rate limited, wait {retry_after}s")
                    if resp.status >= 500:
                        raise aiohttp.ClientError(f"Server error: {resp.status}")
                    if resp.status != 200:
                        raise Exception(f"Matrix API error: {resp.status}")
                    result = await resp.json()
                    return result

        try:
            return await _retry_async(_do_get, tags=f"get_room_state({room_id})", logger=self.logger)
        except Exception as e:
            self.logger.error(f"Failed to get room state after retries: {self._sanitize_url(str(e))}")
            raise

    async def get_room_info(self, room_id: str) -> Dict[str, Any]:
        """Get information about a specific Matrix room (async).

        Returns the room info dict.
        Raises Exception on failure.
        """
        import aiohttp

        encoded_room_id = self._encode_room_id(room_id)
        url = f"{self.url}/_matrix/client/r0/rooms/{encoded_room_id}"
        headers = {"Authorization": f"Bearer {self.access_token}"}

        async def _do_get():
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 429:
                        retry_after = int(resp.headers.get('Retry-After', 10))
                        raise asyncio.TimeoutError(f"Rate limited, wait {retry_after}s")
                    if resp.status >= 500:
                        raise aiohttp.ClientError(f"Server error: {resp.status}")
                    if resp.status != 200:
                        raise Exception(f"Matrix API error: {resp.status}")
                    result = await resp.json()
                    return result

        try:
            return await _retry_async(_do_get, tags=f"get_room_info({room_id})", logger=self.logger)
        except Exception as e:
            self.logger.error(f"Failed to get room info after retries: {self._sanitize_url(str(e))}")
            raise


# ------------------------------------------------------------------
# Async methods (aiohttp) — for daemon polling
# ------------------------------------------------------------------ #

async def _retry_async(
    func,
    tags: str = "",
    logger: logging.Logger = None,
    max_retries: int = 3,
    base_delay: int = 2,
    max_delay: int = 60,
):
    """Retry an async function with exponential backoff, capped at `max_delay`.

    Default policy (max_retries=3) is appropriate for GETs where eventual
    give-up is acceptable.  Callers that MUST succeed (e.g. message POSTs
    where dropping the message corrupts the delegation contract) should
    pass a much higher `max_retries` — see `send_message` below.
    """
    import aiohttp

    for attempt in range(max_retries):
        try:
            return await func()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if attempt == max_retries - 1:
                if logger:
                    logger.error(
                        f"{tags}: giving up after {max_retries} attempts — "
                        f"last error: {MatrixClient._sanitize_url(str(e))}",
                        exc_info=True,
                    )
                raise
            delay = min(base_delay * (2 ** attempt), max_delay)
            if logger:
                logger.warning(
                    f"{tags}: retry {attempt + 1}/{max_retries} after "
                    f"{MatrixClient._sanitize_url(str(e))} (sleeping {delay}s)"
                )
            await asyncio.sleep(delay)
        except Exception as e:
            # Non-retryable errors: log with stack before propagating so we
            # can see WHY it died.
            if logger:
                logger.error(
                    f"{tags}: non-retryable error: "
                    f"{MatrixClient._sanitize_url(str(e))}",
                    exc_info=True,
                )
            raise


# ------------------------------------------------------------------ #
# Config helpers
# ------------------------------------------------------------------ #

def load_config() -> dict:
    """Load delegator-delegatee.yaml from the first location where it exists."""
    import yaml

    config_path = path_utils.find_config()
    if config_path is None:
        raise FileNotFoundError("No delegator-delegatee.yaml found")

    with open(config_path) as f:
        return yaml.safe_load(f)


def get_client(delegatee_name: str = None) -> MatrixClient:
    """Create a MatrixClient from the loaded config.

    If delegatee_name is given, uses that delegatee's matrix config;
    otherwise uses the delegator's matrix config.
    """
    config = load_config()
    if delegatee_name:
        for d in config.get('delegatees', []):
            if d['name'] == delegatee_name:
                return MatrixClient(d['matrix'])
        raise ValueError(f"Delegatee '{delegatee_name}' not found in config")

    return MatrixClient(config['delegator']['matrix'])
