# Loader hand-off harness

`harness.cpp` compiles the loader's real `source/network/PiSync.cpp` for the host,
with the Wii-only APIs (libogc sockets, USB mounting, GUI prompts) replaced by small
stand-ins from `stubs/`. `../test_handoff.py` runs it against a live `wiipi` service
(fake gadget and drive), so the LAN protocol is tested from both ends without a Wii.

The stubs only declare what `PiSync.cpp` uses; keep their signatures in step with
the real headers under `source/`.
