#!/usr/bin/env python3
"""
patch_slave_select.py

Adds a third source section — Slaves — alongside RF and Network Streams.

  * relay forwards the selected Slave to its own port (10999), so nothing
    shares a port with anything else
  * POST /api/remote/{index}/select points mpv at that port
  * a "Slave Receivers" card lists known Slaves, one row each, clickable

The select endpoint deliberately hands off to the existing start_stream()
rather than reimplementing it. That brings the tune_lock acquire and
hand-off, the transition cover, wait_for_mpv_rendering() with its retry,
and save_last_state() — all machinery that took real debugging to get
right and that a parallel implementation would have to get right again.

KNOWN LIMITATION, deliberate: this reuses the stream path, so current_mode
becomes "stream" and a selected Slave still appears in the Stream panel as
well as its own. The duplicate panel is NOT fixed by this patch — that
needs a distinct "remote" mode, which every current_mode branch (panels,
overlay, resume-from-state) has to learn about. Separate piece of work.

is_rf stays False for the same reason. A Slave sends genuine MPEG-TS and
would ideally get is_rf=True, but that flag is not reachable through
start_stream(), and the loose-buffer path is what has been playing this
camera all evening. Worth revisiting with the mode work, together.

Run from ~/lynx:  python3 patch_slave_select.py
"""

import shutil
import sys
from pathlib import Path

TARGET = Path("lynx_app.py")


def apply(src: str, old: str, new: str, label: str) -> str:
    n = src.count(old)
    if n != 1:
        sys.exit(f"ABORT [{label}]: anchor found {n} times, expected exactly 1")
    print(f"  ok  {label}")
    return src.replace(old, new, 1)


def main():
    if not TARGET.exists():
        sys.exit(f"ABORT: {TARGET} not found — run this from ~/lynx")

    src = TARGET.read_text()
    before = len(src.splitlines())

    if "lynx_relay" not in src:
        sys.exit("ABORT: plumbing patch not applied — run patch_slave_relay_plumbing.py first")
    if "REMOTE_VIDEO_OUT_PORT" in src:
        sys.exit("ABORT: already patched")

    # ── 1. relay gets its own output port ────────────────────────
    src = apply(
        src,
        "slave_relay = lynx_relay.SlaveVideoRelay(ingress_port=REMOTE_VIDEO_INGRESS_PORT)",
        "# Where the relay puts the selected Slave, and the only thing mpv\n"
        "# is ever pointed at for a Slave. Its own port rather than sharing\n"
        "# one with the Picotuner or a stream: two writers on one port is\n"
        "# how you get a picture that is half one source and half another,\n"
        "# with nothing in the logs to say so.\n"
        "REMOTE_VIDEO_OUT_PORT = 10999\n"
        "\n"
        "slave_relay = lynx_relay.SlaveVideoRelay(\n"
        "    ingress_port=REMOTE_VIDEO_INGRESS_PORT,\n"
        "    mpv_port=REMOTE_VIDEO_OUT_PORT,\n"
        ")",
        "relay output port",
    )

    # ── 2. select endpoint ───────────────────────────────────────
    src = apply(
        src,
        '@app.post("/api/streams/refresh", tags=["Streaming"],',
        '''@app.post("/api/remote/{index}/select", tags=["Streaming"],
          summary="Display a Slave Rx",
          description="Routes the chosen Slave's video to mpv. The Slave must be "
                      "enabled and must have been heard from, since its video is "
                      "identified by the address its status arrives from.")
def select_remote_source(index: int):
    if index < 0 or index >= len(remote_states):
        raise HTTPException(status_code=404, detail=f"No Slave at index {index}")
    st = remote_states[index]
    if not st["enabled"]:
        raise HTTPException(status_code=409, detail="That Slave is configured but not enabled")
    # Refused rather than attempted: without an address the relay cannot
    # tell this Slave's packets from any other's, so selecting it would
    # produce a black screen with nothing to explain it.
    if not st["addr"]:
        raise HTTPException(status_code=409,
                            detail="Nothing heard from that Slave yet — it must send status before its video can be identified")
    if not slave_relay.stats()["running"]:
        raise HTTPException(status_code=503,
                            detail=f"Slave video relay is not running: {slave_relay.bind_error or 'not started'}")

    slave_relay.select(index)
    # Handing off to start_stream() rather than restarting mpv here: it
    # already holds the lock discipline, the transition cover and the
    # render confirmation, and a second copy of that sequence would be a
    # second place for it to go wrong.
    return start_stream(StreamRequest(
        url=f"udp://@:{REMOTE_VIDEO_OUT_PORT}",
        name=remote_display_name(index),
    ))


@app.post("/api/streams/refresh", tags=["Streaming"],''',
        "select endpoint",
    )

    # ── 3. Slaves card, directly after the Streams card ──────────
    src = apply(
        src,
        """                                    onclick="saveStreamMemory(document.getElementById('custom-url').value.trim(), '')">&#x1F4BE;</button>
                        </div>
                    </div>
                </div>
            </div>
""",
        """                                    onclick="saveStreamMemory(document.getElementById('custom-url').value.trim(), '')">&#x1F4BE;</button>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Slave Receivers -->
            <div class="card mt-3">
                <div class="card-header">&#x1F4E1; Slave Receivers</div>
                <div class="card-body p-0">
                    <div id="slave-list" style="max-height: 300px; overflow-y: auto;">
                        <div class="text-muted small p-3">Loading Slaves...</div>
                    </div>
                </div>
            </div>
""",
        "Slaves card HTML",
    )

    # ── 4. loader + click handler, beside the stream list JS ─────
    src = apply(
        src,
        "async function refreshLiveStreams() {",
        '''// ── Slave Receivers ──────────────────────────────────────────
// Reads the same /api/status the panels read rather than its own
// endpoint: the list and the panels then cannot disagree about which
// Slaves exist or what they are called.
async function loadSlaves() {
    const el = document.getElementById('slave-list');
    if (!el) return;
    try {
        const s = await api('GET', '/api/status');
        const remotes = s.remotes || [];
        if (!remotes.length) {
            el.innerHTML = '<div class="text-muted small p-3">No Slaves configured</div>';
            return;
        }
        el.innerHTML = remotes.map(function (r) {
            // Three states, same meanings as the panel badges: video
            // arriving, heard from but no video, nothing at all.
            var live = r.video && r.video.live;
            var badge = !r.online
                ? '<span class="badge bg-danger" style="font-size:0.65em">OFFLINE</span>'
                : (live
                    ? '<span class="badge bg-success" style="font-size:0.65em">VIDEO</span>'
                    : '<span class="badge bg-warning text-dark" style="font-size:0.65em">NO VIDEO</span>');
            // Only a Slave that is actually sending video can be chosen —
            // selecting one that is not would just black the screen.
            var clickable = r.enabled && r.online && live;
            var kbps = live ? (r.video.kbps + ' kbps') : '';
            return '<div class="stream-item p-2 border-bottom border-secondary d-flex justify-content-between align-items-center"'
                 + (clickable ? ' style="cursor:pointer" onclick="playSlave(' + r.index + ')"'
                              : ' style="opacity:0.55"')
                 + '><span class="small text-light">' + (r.name || ('Slave Rx ' + (r.index + 1)))
                 + (r.callsign ? ' <span class="text-muted">' + r.callsign + '</span>' : '')
                 + '</span><span class="d-flex align-items-center gap-2">'
                 + '<span class="text-muted small">' + kbps + '</span>' + badge + '</span></div>';
        }).join('');
    } catch (e) {
        el.innerHTML = '<div class="text-danger small p-3">Slave list unavailable</div>';
    }
}

async function playSlave(index) {
    try {
        await api('POST', '/api/remote/' + index + '/select');
    } catch (e) {
        alert('Could not select that Slave: ' + e);
    }
}

async function refreshLiveStreams() {''',
        "loadSlaves() + playSlave()",
    )

    # ── 5. init, on its own timer ────────────────────────────────
    src = apply(
        src,
        "loadLiveStreams();\nloadVolume();",
        "loadLiveStreams();\n"
        "loadSlaves();\n"
        "// Its own interval rather than a call inside updateStatus(): the\n"
        "// comment on QuickLynx below applies for the same reason, and a\n"
        "// fault in this list must not be able to stop the status poll.\n"
        "setInterval(loadSlaves, 5000);\n"
        "loadVolume();",
        "init loadSlaves()",
    )

    shutil.copy2(TARGET, TARGET.with_suffix(".py.bak2"))
    TARGET.write_text(src)

    after = len(src.splitlines())
    print(f"\n  lynx_app.py: {before} -> {after} lines (+{after - before})")
    print("  backup: lynx_app.py.bak2")
    print("\nNow:")
    print("  python3 -m py_compile lynx_app.py")
    print("  then extract the served page's JS and run node --check on it")


if __name__ == "__main__":
    main()
