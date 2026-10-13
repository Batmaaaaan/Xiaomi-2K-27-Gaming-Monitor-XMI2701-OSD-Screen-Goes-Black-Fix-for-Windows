# Xiaomi "Mi Monitor" (XMMNT27HQ, EDID XMI 0x2701): OSD black-screen repair on Windows

Tested successfully on **one monitor with EDID manufacturer XMI, product 0x2701** (model XMMNT27HQ, EDID name "Mi Monitor"), on 2026-10-14. Pressing the menu button caused a brief black screen without opening the OSD. Clearing bit `0x10` of EEPROM byte **`0x55`**, then immediately unplugging the power cable, restored the menu; the owner confirmed it works.

## Sources and credit

This is an adaptation of earlier work, not original research into the protocol:

- [supermar1010's Linux script and firmware research](https://gist.github.com/supermar1010/3e81e3f23ce16c826e383edbf9442351): the MStar vendor EEPROM protocol, the stuck DDC power flag, and the "unplug immediately" procedure (Curved 34", product `0x3444`).
- [rm-killer's Windows repair](https://github.com/rm-killer/xiaomi-curved-34-osd-fix-windows) (MIT): the NVIDIA NVAPI transport over HDMI, and the shifted layout of revision `0x3445`.
- [leag's XMI3446 adaptation](https://gist.github.com/leag/3b8234b3f217489eb279eb8ce174a307): the guard-everything approach and the documented EEPROM header.

The `0x2701` layout below was found by reading the monitor, not by disassembling its firmware. It does not establish compatibility with every `0x2701` unit or firmware revision.

## Symptom

- Pressing the menu button blacks out the screen briefly, and no OSD appears.
- Per the earlier research, this is caused by a stuck "DDC power-down pending" flag in the settings EEPROM, set when DDC/CI software sends a power command (VCP `0xD6`, "turn display off"). Ordinary power-cycling does not clear it. I did not verify here what triggered it on the repaired unit.

## What is different on 0x2701

The gists' address mapping does not work on this model. A dump taken with the XMI34 offset returned a 68-byte settings record repeated over and over, which is not an EEPROM image. A read-only scan of the request space found the real layout:

- **EEPROM address 0 is at request value `0x72C3`.** It holds exactly the 16-byte header the XMI34 gists use:
  `00 10 00 10 00 10 03 80 80 80 80 80 80 46 4b 80`. So request value = `0x72C3 + EEPROM address`.
- The request space is 15 bits wide: request `0x8000` mirrors `0x0000`.
- Requests `0x0000`-`0x03FF` hold an MStar secure store (an ASCII magic string, then high-entropy data). **Never write there, and do not publish dumps of it.**
- The settings block has the same shape as the XMI34 one, shifted to EEPROM `0x54`:

```
XMI3446  EEPROM 0x40 (gist) : 02 10 00 00 00 00 00 00 00 00 00 00 00 01 00 00
XMI 2701 EEPROM 0x54 (here) : 06 10 00 00 00 00 00 00 00 00 00 00 00 01 00 01
                                 ^^ stuck flag, bit 0x10 of byte 0x55
```

The script writes this block with bit `0x10` of its second byte cleared (`06 00 00 …`) and changes nothing else. A second `0x10` byte at EEPROM `0x52` is left alone.

## Requirements

- Windows 10/11, an **NVIDIA** GPU with its normal driver, Python 3 (standard library only).
- The monitor connected **directly by HDMI** to the NVIDIA card. DisplayPort and USB-C adapters usually filter these vendor packets, and AMD/Intel graphics will not work with this script.

## Usage

Download `mi_monitor_osd_fix_2701.py`. Check first (writes only a backup file):

```
python mi_monitor_osd_fix_2701.py
```

It verifies the EDID (`XMI 0x2701`), the exact EEPROM header, and the exact settings block, saves a 1 KB backup of EEPROM `0x000-0x3FF`, and reports whether bit `0x10` is set. If a check fails, do not bypass it. Use `--display N` if you have several monitors.

**Have the monitor's physical power cable within reach**, then:

```
python mi_monitor_osd_fix_2701.py --write
```

Type `WRITE` to confirm. As soon as `OK, written and verified` appears and the beeps sound:

1. **Immediately unplug the monitor's power cable**, not the power button.
2. Wait 10 seconds, then plug it back in.
3. Press the menu button.

The monitor periodically saves its RAM settings back to the EEPROM, which would undo the repair if power stays connected.

If the write misbehaves, the script re-reads the whole backed-up range and tries to undo what changed. To put the original block back from a backup later:

```
python mi_monitor_osd_fix_2701.py --restore mi2701_eeprom_backup_<date>.bin
```

## Prevent recurrence

Disable "turn display off" and any power/backlight-off feature in Monitorian, Twinkle Tray, ClickMonitorDDC, ControlMyMonitor and similar tools. Brightness and contrast (VCP `0x10`, `0x12`) are fine. Never send VCP `0xD6` to this monitor.

## Limitations and risks

- This writes the monitor's settings EEPROM and is provided without any guarantee. A wrong layout can corrupt the monitor's configuration.
- Confirmed on **one** unit. The script's exact-match guards limit it to the observed configuration; they are not a firmware signature.
- Tested offline against a simulated monitor (good write, rejected write, misplaced write, wrong header, wrong pattern), and on the one real unit.
- Untested: Linux. If you adapt the Linux script, the request value would be `(EEPROM address - 0x8D3D) & 0xFFFF` (i.e. `BASE = 0x8D3D`); I have not verified this.
- Personal EEPROM backup and scan files are not included. They contain a secure-storage region and should not be shared.
