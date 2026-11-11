# SRT output

Lynx can send what is on its screen over SRT: the picture with the overlay, chat and PPM on it, and the sound. It is the first step towards Lynx feeding Lynx-Tx directly, and it works as a monitor feed for VLC on any computer on the network.

SRT replaces the old "Picture to another screen" (UDP) output, which has been removed.

## The Screen output card

On the web page, the **Screen output** card has two buttons:

- **HDMI:** The picture on the receiver's own screen, as always. Nothing is sent.
- **SRT:** Lynx draws on a virtual screen of its own, switches its HDMI output off, and sends that virtual screen over SRT. Nothing needs to be plugged into the HDMI socket, and no EDID dummy plug is needed: the virtual screen runs at the chosen rate whatever a monitor offers.

There is deliberately no "both". Capturing a real HDMI screen made the picture judder at 2160p, on the monitor and on the SRT feed alike, while each on its own was smooth.

The other settings on the card:

- **Picture:** The format of the SRT feed: 720p50, 720p25, 1080p50, 1080p25, 2160p25 or 2160p50. On a Raspberry Pi only 720p and 1080p are offered (see below).
- **Coding:** H.265 (HEVC), the default, or H.264. On a Pi, H.264 only.
- **Bit rate (kb/s):** Leave it empty for the default: H.265 2.5, 4 and 12 Mb/s at 720p, 1080p and 2160p; H.264 4, 6 and 16 Mb/s.
- **Latency (ms):** SRT's time to resend lost packets. 500 ms is right on a local network. For a receiver across the internet, use 1000 to 2000 ms: SRT needs about four times the round-trip time.
- **Port:** 9000 unless something else on the network needs it. The address box is only used when Lynx calls the far end.

Under **More SRT settings**:

- **Sound delay (ms):** Leave it at 0. It is there for a far end whose own chain delays the picture or the sound (a mixer, a frame synchroniser, a slow decoder). Plus delays the sound, for a late picture; minus brings the sound earlier.
- **Who connects:** **The far end calls Lynx (listener)**, the default and the recommended setting: Lynx waits on its port, and VLC or the transmitter connects to it. The other choice, **Lynx calls the far end (caller)**, sends to the address and port above, for a far end that listens.
- **Passphrase:** 10 to 79 characters, or empty for none. Shown as dots; the eye button shows it. With a passphrase the stream is encrypted (AES-128), and the far end needs the same phrase. Deleting it switches encryption off.
- **Stream ID:** Only if the far end asks for one.

The line at the foot of the card says what is happening: waiting for a caller, sending and for how long, or what is wrong.

## Watching in VLC

VLC 3 can only call over SRT, so leave Lynx as the listener (the default).

**In VLC:** File, Open Network, and enter

    srt://<receiver address>:9000

for example `srt://192.168.0.106:9000`.

**With a passphrase**, VLC needs it too. Its Open Network box has no passphrase field in VLC 3, so start VLC from a terminal. On a Mac:

    /Applications/VLC.app/Contents/MacOS/VLC "srt://192.168.0.106:9000" --passphrase='your phrase'

On Linux or Windows, the same with `vlc` in place of the path.

SRT connects one viewer at a time: a second VLC cannot join while the first is connected. When a viewer leaves, Lynx is listening again within a second.

A viewer joining between two keyframes sees a broken picture for up to a second, until the next keyframe. VLC on a Mac may say "decoder failure" at that moment and carry on in software; that is normal.

**Not VLC:** Homebrew's FFmpeg and mpv on a Mac are built without SRT. On Linux, `ffplay "srt://192.168.0.106:9000"` works, as does mpv.

## Boards

- **ROCK 5C:** Everything, 2160p50 included. Its hardware encoder does the work, and the screen is captured without a copy through the processor.
- **Raspberry Pi 5:** No hardware encoder, so H.264 in software, 720p and 1080p only: **SRT on a Pi is for sources up to 1080p.** A 4K source still plays on the Pi, but its SRT sound and picture drift apart. Use a ROCK for 4K over SRT.

## Sound

The SRT feed carries the sound Lynx plays: stereo AAC at 192 kb/s. A 5.1 AC-3 programme is mixed down to stereo by the player before it is sent. The PPM follows the sound, on HDMI and on SRT.

## If it does not work

On the receiver:

    tail -30 /tmp/lynx-srt.log
    grep '\[srt\]' /tmp/lynx_app.log | tail

- **"missing on this receiver: wf-recorder":** The installer has not built the screen capture. Run Update Now, or the installer again.
- **"Connection ... failed" in caller mode:** Nothing is listening at the far end, or a firewall stops it. In listener mode the far end does the calling, and a firewall at the far end does not matter.
- **No picture in VLC with a passphrase:** The phrase differs, or VLC was not given it (see above).
