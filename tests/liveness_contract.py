"""Shared platform-liveness contracts for durable-supervisor conformance.

These tests intentionally exercise the shared durable-runner primitives rather
than any provider adapter.  An uncertain identity must never authorize cleanup,
and a Linux zombie is terminal only after it is reaped by its owning parent.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from unittest import mock

from recollect_lines.durable_runner import classify_process_identity, read_process_start_identity


class NonLinuxStartIdentityFallbackTests(unittest.TestCase):
    """Non-Linux fallback identity is evidence of uncertainty, never death."""

    def test_fallback_identity_of_a_live_process_is_never_reported_dead(self):
        pid = os.getpid()
        with mock.patch("recollect_lines.durable_runner.sys.platform", "darwin"):
            captured_at_launch = read_process_start_identity(pid)
            self.assertIsNotNone(captured_at_launch)
            self.assertFalse(captured_at_launch.startswith("linux:"))
            result = classify_process_identity(pid, captured_at_launch)
        self.assertEqual(result, "unknown")

    def test_a_genuinely_dead_pid_is_still_reported_dead_on_non_linux(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait(timeout=5)
        with mock.patch("recollect_lines.durable_runner.sys.platform", "darwin"):
            result = classify_process_identity(proc.pid, f"darwin:pid={proc.pid}:monotonic=123")
        self.assertEqual(result, "dead")

    def test_linux_identity_comparison_is_unaffected(self):
        if sys.platform != "linux":
            self.skipTest("Linux-specific anti-PID-reuse identity check")
        pid = os.getpid()
        identity = read_process_start_identity(pid)
        self.assertTrue(identity.startswith("linux:"))
        self.assertEqual(classify_process_identity(pid, identity), "alive")
        self.assertEqual(classify_process_identity(pid, "linux:boot=deadbeef:starttime=999999999"), "dead")


class LinuxZombieIdentityContractTests(unittest.TestCase):
    """A Linux zombie is terminal and must be reaped by its owning parent."""

    @unittest.skipUnless(sys.platform == "linux", "Linux /proc zombie contract")
    def test_zombie_is_not_live_and_is_reaped_by_owner(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        identity = read_process_start_identity(proc.pid)
        self.assertTrue(identity and identity.startswith("linux:"))
        # waitpid(WNOHANG) observes terminal exit and performs the required reap.
        observed_pid, _status = os.waitpid(proc.pid, 0)
        self.assertEqual(observed_pid, proc.pid)
        self.assertEqual(classify_process_identity(proc.pid, identity), "dead")
