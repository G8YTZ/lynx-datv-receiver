# Lynx Remote Rx

A Remote Rx is a small receiver at another site, built from a Raspberry Pi Zero 2 W and an unmodified tuner. It tunes one DATV signal and sends the picture back to a Lynx receiver at the repeater over SRT, along with its lock, quality readings and callsign. At the repeater it appears as one more receiver, with its own panel and a Watch button.

It needs no ports opening at the remote site: The Remote Rx always calls out to the repeater. Once it is built, everything else (frequency, the second tuning figure, name and locator) is set from the Lynx at the repeater, and the Remote Rx keeps those settings through a power cut.

## Which tuner

Two are supported, and the Remote Rx works out for itself which one is plugged in at every start. Swapping one for the other needs a service restart and nothing else.

| | **Minitiouner** | **Knucker** (BATC CombiTuner) |
|---|---|---|
| Receives | DVB-S / DVB-S2 | DVB-T / DVB-T2, 150 kHz to 2 MHz |
| Second tuning figure | Symbol rate, kS/s | Channel bandwidth, kHz |
| Reports | MER, margin, calibrated level in dBm, modcod, callsign, codecs | SNR in dB, level and SNQ as percentages, PER, standard, mode, callsign, codecs |
| Driver | Longmynd, open source, built by the installer | CombiTunerExpress, a closed 32-bit binary from the BATC |
| Extra setup | None | The armhf architecture and its runtime libraries, added by `install.sh --knucker` |

**The Minitiouner is the usual choice.** DVB-S2 is what most amateur DATV uses, and a Minitiouner card carries nothing unusual. The Knucker is there for narrowband DVB-T2, which the Minitiouner cannot receive at all.

The two report genuinely different measurements, and the panel shows whichever applies rather than converting one into the other. A Knucker's SNR is not a MER and is not displayed as one; its level is a percentage, not a calibrated dBm. Nothing on either panel is a figure its tuner did not measure.

![Off-air picture from a Remote Rx](docs/remote-rx-off-air.jpg)

*A test card from a Portsdown on 438 MHz, received by the Remote Rx (JO01IF) and shown on the Lynx at home over SRT: The overlay says **REMOTE** and the Remote Rx's locator.*

![Remote Rx wiring](docs/remote-rx-wiring.svg)

## What you need

| Part | Notes |
|---|---|
| Raspberry Pi Zero 2 W | The original Zero is too slow. |
| microSD card, 8 GB or larger | |
| 5 V 2.5 A micro-USB power supply | The official Zero supply is ideal. |
| Minitiouner **or** Knucker | See [Which tuner](#which-tuner). A standard Minitiouner, with its own DC supply: Do not try to power it from the Pi. A Picotuner (the non-Ethernet one) is not supported yet: It presents differently over USB and has not been tested here. |
| Micro-USB to mini-USB OTG cable | Micro-USB plug for the Zero, mini-USB plug for the Minitiouner, sold as "OTG", [for example this one](https://www.amazon.co.uk/dp/B0D9HMQQY9). Or use the Minitiouner's usual USB cable with an OTG adapter (micro-USB plug to USB-A socket). |
| Masthead LNA for the band | Mounted at the antenna. |
| Bias-T and a supply for the LNA | Feeds DC up the coax to the LNA. |
| SAW band-pass filter for the band | Protects the Minitiouner's front end from strong signals outside the band. |

## Wiring

On a **Knucker**, the RF goes to the **LOWER** F connector, the one nearest the board: That is the terrestrial input. The upper one is for satellite and does nothing in this software. Otherwise the wiring below is the same, reading "Knucker" for "Minitiouner".

1. **Antenna to Minitiouner:** Antenna, masthead LNA, coax, bias-T, SAW filter, then the Minitiouner's **TOP** RF input. The Minitiouner never puts voltage on its RF input here (Lynx doesn't turn on LNB power), so the bias-T is what powers the LNA. Put the bias-T between the coax and the SAW filter: Most SAW filters block DC.
2. **Minitiouner to the Pi:** The micro-USB to mini-USB OTG cable, from the Minitiouner's mini-USB socket to the Zero's **middle** micro-USB port, marked USB. That's the data port.
3. **Power:** The 5 V supply goes into the Zero's **corner** micro-USB port, marked PWR IN. The Minitiouner gets its own DC supply as usual.
4. **Network:** Wi-Fi to the router at the remote site. The Zero 2 W only does 2.4 GHz Wi-Fi.

**One input only.** Longmynd drives one input of the Minitiouner's NIM, and the Remote Rx uses the TOP one. The BOTTOM input isn't used. To receive two bands at the same site, build two Remote Rx.

**Which filter goes where:** The LNA sets the noise figure, so it goes first, at the antenna. The SAW filter after it stops anything strong outside the band (broadcast TV, mobile phone masts, pagers) from overloading the Minitiouner. If a very strong local transmitter overloads the LNA itself, you need a filter in front of it as well.

## Step 1: Prepare the SD card

Use Raspberry Pi Imager on any computer.

1. **Device:** Raspberry Pi Zero 2 W.
2. **Operating system:** Raspberry Pi OS (other), then **Raspberry Pi OS Lite (64-bit)**. No desktop is needed.
3. **Storage:** Your SD card.
4. When asked, choose **Edit settings** and fill in:
   - **Hostname:** For example `lynx-remote-rx`.
   - **Username:** `pi`. It must be `pi`: The installer and the service expect `/home/pi`.
   - **Password:** Your choice.
   - **Wireless LAN:** The Wi-Fi name and password at the site where it will live, and the country (GB).
   - **Services:** Tick **Enable SSH**, with password authentication.
5. Write the card, put it in the Zero, connect everything and power up.

The first boot takes a few minutes. If you are building it at home to take to another site later, enter your home Wi-Fi now and add the remote site's Wi-Fi afterwards (see "Moving it to another site" below).

## Step 2: Log in

From a computer on the same network:

```
ssh pi@lynx-remote-rx.local
```

If the name isn't found, look up the Zero's address in your router's device list and use `ssh pi@<address>` instead.

## Step 3: Install

Copy and paste the whole block in one go:

```
sudo apt update &&
sudo apt full-upgrade -y &&
sudo apt install -y git &&
git clone --depth 1 --filter=blob:none --sparse -b beta https://github.com/G8YTZ/lynx-datv-receiver.git ~/lynx-datv-receiver &&
git -C ~/lynx-datv-receiver sparse-checkout set slave &&
~/lynx-datv-receiver/slave/install.sh
```

Each step runs only if the one before it worked, so a failure stops there
rather than carrying on against a half-built system. Expect it to take a
while on a Zero 2 W: the upgrade and the Longmynd build are both slow.
Watch the end of it, because the installer prints this Remote Rx's key.

This fetches only the Remote Rx part of Lynx, not the whole receiver. The installer then:

- installs `srt-tools`;
- downloads and builds **Longmynd**, the BATC's Minitiouner driver, in `~/longmynd`;
- installs the udev rule that lets it use the Minitiouner without root;
- **for a Knucker only:** adds the armhf architecture and its runtime libraries, and fetches **CombiTunerExpress** into `~/knucker`. This happens automatically when a Knucker is plugged in while the installer runs. To build the card before the tuner arrives, add `--knucker`:

  ```
  ~/lynx-datv-receiver/slave/install.sh --knucker
  ```

  A Minitiouner card does not get any of this, and should not: It is 30-50 MB of 32-bit libraries for a tuner it will never have;
- creates the settings file `/etc/lynx-slave.conf`;
- makes this Remote Rx's **key** and prints it: **Write it down**, you need it at the repeater;
- starts the service, which runs at every boot from then on.

Finish with `sudo reboot`, so the udev rule applies to the Minitiouner.

## Step 4: Tell it where the repeater is

```
sudo nano /etc/lynx-slave.conf
```

Set `LYNX_HOST` to the address of the Lynx at the repeater: Its address on the SD-WAN, or the repeater's public address or DNS name if it comes over the internet. Check `TRANSPORT="srt"`. Then press Ctrl-O, Enter, Ctrl-X to save, and restart:

```
sudo systemctl restart lynx-slave
```

The frequency, the second tuning figure (symbol rate for a Minitiouner, bandwidth for a Knucker), name and locator in this file are only used until the repeater sends its own, so you can leave them.

## Step 5: Set it up at the repeater

On the Lynx at the repeater, open the web page, then **Config**, **Remote Rx**:

1. Tick **Enabled**.
2. Under **Settings sent to the Remote Rx**, enter the frequency (MHz), the second tuning figure, the name and the locator. That box is labelled **Symbol rate (kS/s)** for a Minitiouner and relabels itself to **Bandwidth (kHz)** once a Knucker has reported in.
3. Paste the **key** from step 3.
4. Click **Save**.

Within a few seconds the Remote Rx panel appears with Settings: **Applied**, and the Remote Rx retunes.

![The Remote Rx panel at the repeater](docs/remote-rx-panel.png) In the **Remote Receivers** box on the main page, click **Watch** to put it on screen. The overlay shows **REMOTE** and the locator.

### Ports at the repeater

If the Remote Rx comes in over the internet rather than an SD-WAN, the repeater's router must forward these **UDP** ports to the Lynx receiver:

| Port | What |
|---|---|
| 10947 | SRT video |
| 10997 | Status (lock, callsign) and the settings sent back |
| 10901 | Quality (MER and level, or SNR and SNQ) |

Nothing needs opening at the remote site.

## Checking it

On the Remote Rx:

```
journalctl -u lynx-slave -n 30 --no-pager
```

What good looks like:

- `config : remote, settings version …`: It is using the settings from the repeater.
- `SRT target connected`: The video link is up.
- `[tuner] locked - G8YTZ 438.021 MHz MER 24.2 dB`: It has a signal and can read the callsign.

At the repeater, the Remote Rx panel shows the link's round-trip time, data rate and the SRT lost, resent and dropped counts. Resent packets are normal on the internet; dropped packets mean the SRT latency is too short for the link.

## Updating

Normally there is nothing to do here: On the Lynx at the repeater, the
Remote Rx panel shows the version it is running, and an **Update**
button appears when this receiver is on a newer one. Pressing it sends
a signed command in reply to the Remote Rx's next heartbeat. The Remote
Rx fetches, refuses the update unless the new code compiles and starts,
then restarts. If the new version will not stay running it puts the old
one back by itself.

**The first update must be done by hand.** A Remote Rx built before the
Update button existed has no code to act on the command, so it ignores
it. Once, on the Remote Rx:

```
cd ~/lynx-datv-receiver && git pull && sudo systemctl restart lynx-slave
```

After that the button works. The installer is safe to run again and
keeps your settings and your key.

**Changing tuner needs no reinstall**, even on a Remote Rx built before
tuner detection existed. Such a card has a wrapper that always starts
the Minitiouner source, and that wrapper is outside the update path, so
it cannot be replaced remotely. The Minitiouner source therefore checks
for itself and hands over to the right one. Press **Update**, plug in
the other tuner, restart the service, and it follows.

Worth running `./install.sh` once anyway when convenient: it replaces
that wrapper with a thin launcher that defers to the checkout, so from
then on changes to how the service starts arrive by the Update button
like everything else. Nothing depends on it; it is the difference
between a card that can be fixed remotely in future and one that may
need another ssh session.

If you are changing the Remote Rx code rather than running it, read
[UPDATE-DISCIPLINE.md](UPDATE-DISCIPLINE.md) first: the rules that keep
the Update button working on units nobody can reach, and what each one
cost to learn.

## Moving it to another site

Add the new site's Wi-Fi before you take it there:

```
sudo nmcli dev wifi connect "SITE-WIFI-NAME" password "SITE-WIFI-PASSWORD"
```

The Zero remembers both networks and joins whichever it finds. Change `LYNX_HOST` too if the repeater's address is different from the new site.

## Troubleshooting

| What you see | Likely cause |
|---|---|
| No Remote Rx panel at the repeater | It isn't ticked **Enabled** in Config, or the status port (10997) isn't reaching the Lynx. |
| Panel shows offline | The Zero isn't running or has no network. Check Wi-Fi, and `systemctl status lynx-slave`. |
| Locked, but NO VIDEO | SRT isn't connecting: Check port 10947 at the repeater, and `LYNX_HOST`. |
| Settings: No key on the Remote Rx | The key line is missing from `/etc/lynx-slave.conf`. Run `./install.sh` again. |
| Settings stay "Sending…" | The keys don't match. Copy the `KEY` from `/etc/lynx-slave.conf` into Config again. |
| Never locks | Wrong frequency or symbol rate, aerial on the wrong input (**TOP** on a Minitiouner, **LOWER** on a Knucker), or no power to the LNA. |
| `longmynd` can't open the Minitiouner | Unplug and replug it, or reboot, so the udev rule applies. |
| Knucker: `cannot execute: required file not found` | The armhf libraries are missing. Run `./install.sh --knucker`. |
| Knucker: `No CombiTunerExpress at ...` | The driver was never fetched. Run `./install.sh --knucker`. |
| Knucker: exits with `rc=-11` | Another copy is still holding the USB device: It segfaults rather than saying so. `pgrep -a CombiTunerExpress`, kill any stray one, and restart the service. |
| Knucker: `DVBTx channel scan is fail` over and over | The tuner is behaving: It locks its own synthesiser (`Tuner locked!` means only that) and finds nothing to demodulate. Check the transmitter is actually radiating, that the bandwidth matches at both ends, and that the coax is on the LOWER connector. |
| Knucker: locked, but no callsign | The transmitter is not putting a service name in its SDT. Nothing to fix at this end. |
| Wrong tuner detected | `python3 ~/lynx-datv-receiver/slave/detect_tuner.py -v` prints the USB product string it found. Force it with `TUNER=knucker` or `TUNER=minitiouner` in `/etc/lynx-slave.conf`. |

To go back to the frequency and name in `/etc/lynx-slave.conf` instead of the repeater's, delete `/var/lib/lynx-slave/settings.json` and restart the service.
