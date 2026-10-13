#!/usr/bin/env python3
"""Xiaomi "Mi Monitor" (EDID XMI 0x2701) OSD black-screen fix, Windows / NVIDIA NVAPI / HDMI.

Derived from a read-only dump of one monitor; the owner reported the OSD working again after
the repair (one unit, 2026-10-14). Not a firmware signature: read the evidence and the risks
below before using --write.

Credit: transport from rm-killer's Windows repair (MIT)
  https://github.com/rm-killer/xiaomi-curved-34-osd-fix-windows
MStar vendor protocol + stuck-flag research by supermar1010
  https://gist.github.com/supermar1010/3e81e3f23ce16c826e383edbf9442351
XMI3446 adaptation (layout inferred from matching data, then confirmed on hardware)
  https://gist.github.com/leag/3b8234b3f217489eb279eb8ce174a307

EVIDENCE for this monitor (from a read-only scan of the monitor itself):
  * Request 0x72C3 holds exactly the 16-byte EEPROM header the gists use:
        00 10 00 10 00 10 03 80 80 80 80 80 80 46 4b 80
    so EEPROM address A maps to request value 0x72C3 + A on this firmware.
  * At A=0x54 there is a 16-byte block with the same shape as the gists' settings block
    (stuck flag = bit 0x10 of byte 1, then 11 zero bytes, then 01):
        gist XMI3446 A=0x40 : 02 10 00 00 00 00 00 00 00 00 00 00 00 01 00 00
        this monitor A=0x54 : 06 10 00 00 00 00 00 00 00 00 00 00 00 01 00 01
    The flag would therefore be bit 0x10 of EEPROM byte 0x55 (request 0x7318).
  * The firmware was not disassembled, so this is an inference by pattern, later confirmed by
    a successful repair on one unit. A second 0x10 byte exists at A=0x52 and is NOT touched.

Modes (display index with --display N, default 0):
  python mi_monitor_fix_2701.py                    check only, writes nothing except a backup file
  python mi_monitor_fix_2701.py --write            apply the one-bit fix (asks you to type WRITE)
  python mi_monitor_fix_2701.py --restore FILE     put the original 16-byte block back from a backup

After a successful --write you MUST IMMEDIATELY pull the monitor's POWER CABLE (not the
power button), wait 10 s, plug it back in. The monitor periodically saves its old RAM
settings back to the EEPROM and would undo the fix otherwise.
Requirements: Windows, NVIDIA GPU + driver, monitor DIRECTLY on HDMI, Python 3.
Use at your own risk: this writes the monitor's settings EEPROM.
"""
import ctypes
import os
import sys
import time

NVAPI_INITIALIZE = 0x0150E828
NVAPI_ENUM_NVIDIA_DISPLAY_HANDLE = 0x9ABDD40D
NVAPI_GET_ASSOCIATED_DISPLAY_OUTPUT_ID = 0xD995937E
NVAPI_GET_PHYSICAL_GPUS_FROM_DISPLAY = 0x34EF9506
NVAPI_I2C_READ = 0x2FDE12C5
NVAPI_I2C_WRITE = 0xE812EB07

EDID_HEADER = b"\x00\xff\xff\xff\xff\xff\xff\x00"
EDID_VENDOR, EDID_PRODUCT = "XMI", 0x2701

EEPROM_ORIGIN = 0x72C3            # request value of EEPROM address 0 on this firmware
HEADER = bytes.fromhex("00 10 00 10 00 10 03 80 80 80 80 80 80 46 4b 80")
BLOCK_A = 0x54                    # 16-byte block containing the flag
FLAG_OFF = 1                      # flag byte = EEPROM 0x55
BIT = 0x10
EXPECTED = bytes.fromhex("06 10 00 00 00 00 00 00 00 00 00 00 00 01 00 01")
BACKUP_LEN = 0x400                # EEPROM 0x000-0x3FF are backed up

SLEEP = time.sleep                # replaced in tests


class Abort(Exception):
    pass


def die(msg):
    raise Abort(msg)


# --------------------------------------------------------------------------- transport
class NV_I2C_INFO_V3(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_uint32),
        ("displayMask", ctypes.c_uint32),
        ("bIsDDCPort", ctypes.c_uint8),
        ("i2cDevAddress", ctypes.c_uint8),
        ("pbI2cRegAddress", ctypes.POINTER(ctypes.c_uint8)),
        ("regAddrSize", ctypes.c_uint32),
        ("pbData", ctypes.POINTER(ctypes.c_uint8)),
        ("cbSize", ctypes.c_uint32),
        ("i2cSpeed", ctypes.c_uint32),
        ("i2cSpeedKhz", ctypes.c_uint32),
        ("portId", ctypes.c_uint8),
        ("bIsPortIdSet", ctypes.c_uint32),
    ]


class Nv:
    def __init__(self):
        nv = ctypes.windll.LoadLibrary("nvapi64.dll")
        q = nv.nvapi_QueryInterface
        q.restype = ctypes.c_void_p
        q.argtypes = [ctypes.c_uint32]
        ctypes.CFUNCTYPE(ctypes.c_int)(q(NVAPI_INITIALIZE))()
        self.enum_disp = ctypes.CFUNCTYPE(
            ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)
        )(q(NVAPI_ENUM_NVIDIA_DISPLAY_HANDLE))
        self.get_out_id = ctypes.CFUNCTYPE(
            ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)
        )(q(NVAPI_GET_ASSOCIATED_DISPLAY_OUTPUT_ID))
        self.get_gpus = ctypes.CFUNCTYPE(
            ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)
        )(q(NVAPI_GET_PHYSICAL_GPUS_FROM_DISPLAY))
        self.i2c_read = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(q(NVAPI_I2C_READ))
        self.i2c_write = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(q(NVAPI_I2C_WRITE))

    def displays(self):
        out = []
        for i in range(32):
            h = ctypes.c_void_p()
            if self.enum_disp(i, ctypes.byref(h)) != 0:
                break
            oid = ctypes.c_uint32(0)
            self.get_out_id(h, ctypes.byref(oid))
            gpus = (ctypes.c_void_p * 64)()
            cnt = ctypes.c_uint32(0)
            self.get_gpus(h, gpus, ctypes.byref(cnt))
            out.append((i, gpus[0], oid.value))
        return out


def _info(mask, dev_addr, reg, buf, n):
    info = NV_I2C_INFO_V3()
    info.version = ctypes.sizeof(NV_I2C_INFO_V3) | (3 << 16)
    info.displayMask = mask
    info.bIsDDCPort = 1
    info.i2cDevAddress = dev_addr
    if reg is None:
        info.pbI2cRegAddress = None
        info.regAddrSize = 0
    else:
        info.pbI2cRegAddress = ctypes.pointer(reg)
        info.regAddrSize = 1
    info.pbData = ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint8))
    info.cbSize = n
    info.i2cSpeed = 0xFFFF
    info.i2cSpeedKhz = 0
    return info


def read_edid(nv, gpu, mask):
    reg = ctypes.c_uint8(0x00)
    buf = (ctypes.c_uint8 * 128)()
    info = _info(mask, (0x50 << 1) | 1, reg, buf, 128)
    if nv.i2c_read(gpu, ctypes.byref(info)) != 0:
        return None
    return bytes(buf)


def describe_edid(edid):
    mfg = edid[8] << 8 | edid[9]
    vendor = "".join(chr(((mfg >> s) & 0x1F) + 64) for s in (10, 5, 0))
    return vendor, edid[10] | edid[11] << 8


def _xor_frame(frame):
    chk = 0x6E
    for b in frame:
        chk ^= b
    return chk


class NvIo:
    """Vendor EEPROM access. `want` is the 16-bit request value."""

    def __init__(self, nv, gpu, mask):
        self.nv, self.gpu, self.mask = nv, gpu, mask

    def read(self, want):
        hi, lo = (want & 0xFFFF) >> 8, want & 0xFF
        chk = _xor_frame([0x51, 0x86, 0x03, 0x7B, 0x99, 0x22, hi, lo])
        for _ in range(3):
            reg = ctypes.c_uint8(0x51)
            data = (ctypes.c_uint8 * 8)(0x86, 0x03, 0x7B, 0x99, 0x22, hi, lo, chk)
            w = _info(self.mask, 0x37 << 1, reg, data, 8)
            if self.nv.i2c_write(self.gpu, ctypes.byref(w)) == 0:   # read REQUEST only
                SLEEP(0.08)
                rb = (ctypes.c_uint8 * 20)()
                r = _info(self.mask, (0x37 << 1) | 1, None, rb, 20)
                if self.nv.i2c_read(self.gpu, ctypes.byref(r)) == 0:
                    raw = bytes(rb)
                    x = 0x50
                    for b in raw[:19]:
                        x ^= b
                    if raw[0] == 0x6E and raw[1] == 0x92 and x == 0:
                        return raw[2:18]
            SLEEP(0.2)
        return None

    def write(self, want, data16):
        """Vendor command 7B 99 55: write 16 bytes. Returns True if the transfer was accepted."""
        assert len(data16) == 16
        hi, lo = (want & 0xFFFF) >> 8, want & 0xFF
        chk = _xor_frame([0x51, 0x96, 0x03, 0x7B, 0x99, 0x55, hi, lo, *data16])
        payload = [0x96, 0x03, 0x7B, 0x99, 0x55, hi, lo, *data16, chk]
        reg = ctypes.c_uint8(0x51)
        arr = (ctypes.c_uint8 * len(payload))(*payload)
        w = _info(self.mask, 0x37 << 1, reg, arr, len(payload))
        return self.nv.i2c_write(self.gpu, ctypes.byref(w)) == 0


# --------------------------------------------------------------------------- logic
def want_of(a):
    return (EEPROM_ORIGIN + a) & 0xFFFF


def read_checked(io, a):
    b = io.read(want_of(a))
    if b is None:
        die(f"no valid reply reading EEPROM {a:#05x}. Nothing was changed.")
    return b


def read_stable(io, a):
    x, y = read_checked(io, a), read_checked(io, a)
    if x != y:
        die(f"unstable read at EEPROM {a:#05x}. Nothing was changed.")
    return x


def check_identity(io):
    head = read_stable(io, 0)
    if head != HEADER:
        die(f"EEPROM header is {head.hex(' ')}, not the expected one. This is not the monitor / "
            "firmware this script was derived from. Nothing was changed.")
    print("EEPROM header matches the expected pattern at request 0x72C3: OK")


def make_backup(io, outdir):
    data = b"".join(read_checked(io, a) for a in range(0, BACKUP_LEN, 16))
    name = os.path.join(outdir, f"mi2701_eeprom_backup_{time.strftime('%Y%m%d-%H%M%S')}.bin")
    with open(name, "xb") as f:
        f.write(data)
    print(f"backup of EEPROM 0x000-0x3FF ({len(data)} bytes) saved to {name}")
    return name, data


def block_state(cur):
    """Return True if the flag is set. Abort unless the block is exactly the expected one."""
    exp, obs = bytearray(EXPECTED), bytearray(cur)
    exp[FLAG_OFF] &= ~BIT
    obs[FLAG_OFF] &= ~BIT
    if obs != exp:
        die(f"block at EEPROM {BLOCK_A:#04x} is {bytes(cur).hex(' ')}, which differs from the "
            f"observed pattern {EXPECTED.hex(' ')} (apart from the flag bit). Aborting; "
            "nothing was changed.")
    return bool(cur[FLAG_OFF] & BIT)


def beep():
    try:
        import winsound
        for _ in range(10):
            winsound.Beep(1800, 200)
            SLEEP(0.05)
    except Exception:
        print("\a" * 5)


def cmd_check(io, outdir):
    check_identity(io)
    _, data = make_backup(io, outdir)
    cur = read_stable(io, BLOCK_A)
    if cur != data[BLOCK_A:BLOCK_A + 16]:
        die("block read differs from the backup, unstable. Nothing was changed.")
    set_ = block_state(cur)
    print(f"block at EEPROM {BLOCK_A:#04x}: {cur.hex(' ')}")
    print(f"byte {BLOCK_A + FLAG_OFF:#04x} = {cur[FLAG_OFF]:#04x}; bit 0x10 is "
          f"{'SET (candidate stuck flag)' if set_ else 'already clear'}")
    if not set_:
        print("Nothing to fix.")
    else:
        print("Check only. To apply the one-bit fix run again with --write.")
    return 0


def diff_scan(io, data):
    """Re-read the whole backup range; return {eeprom_address: current_byte} for every byte
    that differs from the backup."""
    diff = {}
    for a in range(0, BACKUP_LEN, 16):
        now = io.read(want_of(a))
        if now is None:
            die("cannot re-read the EEPROM to assess the damage. Keep the backup file and do "
                "not write again.")
        for i in range(16):
            if now[i] != data[a + i]:
                diff[a + i] = now[i]
    return diff


def recover(io, data, cur, new):
    """After a failed write: find what actually changed and put it back, without ever writing
    to an address we have not verified."""
    print("Checking what actually changed (re-reading the backed-up range)...")
    diff = diff_scan(io, data)
    if not diff:
        print("Nothing in EEPROM 0x000-0x3FF changed. The monitor was not modified.")
        return
    shown = ", ".join(f"{a:#05x}:{data[a]:02x}->{v:02x}" for a, v in sorted(diff.items())[:24])
    print(f"Changed bytes (address:old->new): {shown}")
    lo, hi = min(diff), max(diff)
    if lo >= BLOCK_A and hi < BLOCK_A + 16:            # only the intended block: same-path restore
        io.write(want_of(BLOCK_A), cur)
        SLEEP(0.3)
        if io.read(want_of(BLOCK_A)) == cur:
            print("Original block restored.")
        else:
            print("WARNING: could not restore the block. Keep the backup; do not write again.")
        return
    for off in range(-0x40, 0x41):                      # did the write land at a constant offset?
        land = BLOCK_A + off
        if off == 0 or land < 0 or land + 16 > BACKUP_LEN:
            continue
        if lo >= land and hi < land + 16 and bytes(
                diff.get(land + i, data[land + i]) for i in range(16)) == new:
            orig = data[land:land + 16]
            print(f"The write landed {off:+d} bytes away from the target (EEPROM {land:#05x}). "
                  "Restoring that block through the same path.")
            io.write(want_of(BLOCK_A), orig)
            SLEEP(0.3)
            if io.read(want_of(land)) == orig:
                print("Original bytes restored.")
            else:
                print("WARNING: could not restore. Keep the backup; do not write again.")
            return
    print("Unexpected changes that I cannot safely undo automatically. Do NOT write again. "
          "Keep the backup file and send me the list above.")


def cmd_write(io, outdir, input_fn=input):
    check_identity(io)
    _, data = make_backup(io, outdir)
    cur = read_stable(io, BLOCK_A)
    if cur != data[BLOCK_A:BLOCK_A + 16]:
        die("block read differs from the backup, unstable. Nothing was changed.")
    if not block_state(cur):
        print("bit 0x10 is already clear. Nothing to fix.")
        return 0
    new = bytearray(cur)
    new[FLAG_OFF] &= ~BIT
    print("old block:", cur.hex(" "))
    print("new block:", bytes(new).hex(" "))
    print("\n" + "#" * 64)
    print("Tested on one unit only: the flag position was inferred from the data layout.")
    print("Put your hand on the monitor's POWER CABLE now. After the write you must pull it")
    print("immediately (not the power button), wait 10 s, then plug it back in.")
    print("#" * 64)
    if input_fn("Type WRITE (capitals) to write, anything else to abort: ").strip() != "WRITE":
        die("aborted by user. Nothing was changed.")

    ok = io.write(want_of(BLOCK_A), bytes(new))
    SLEEP(0.3)
    back = io.read(want_of(BLOCK_A))
    problems = []
    if not ok:
        problems.append("the write transfer was rejected")
    if back != bytes(new):
        problems.append(f"readback {back.hex(' ') if back else None} != new block")
    for a in (0x00, BLOCK_A - 16, BLOCK_A + 16):       # a misplaced write would show here
        if io.read(want_of(a)) != data[a:a + 16]:
            problems.append(f"EEPROM {a:#05x} changed unexpectedly")
    if problems:
        print("\nPROBLEM: " + "; ".join(problems))
        recover(io, data, cur, bytes(new))
        die("fix not applied. Keep the backup file for investigation.")

    print("\nOK, written and verified; neighbouring blocks unchanged.")
    print(">>> PULL THE MONITOR'S POWER CABLE NOW (not the power button) <<<")
    beep()
    print("Wait 10 s, plug it back in, press the joystick: the OSD should open.")
    print("If it does not, the inferred flag position may differ on your unit; use --restore with your backup.")
    return 0


def cmd_restore(io, backup_file, input_fn=input):
    check_identity(io)
    with open(backup_file, "rb") as f:
        data = f.read()
    if len(data) != BACKUP_LEN or data[:16] != HEADER:
        die("that is not a backup file made by this script. Nothing was changed.")
    orig = data[BLOCK_A:BLOCK_A + 16]
    block_state(orig)  # backup block must look like the expected pattern
    cur = read_stable(io, BLOCK_A)
    block_state(cur)
    if cur == orig:
        print("The block already equals the backup. Nothing to restore.")
        return 0
    print("current:", cur.hex(" "))
    print("backup :", orig.hex(" "))
    if input_fn("Type RESTORE to write the backup block back: ").strip() != "RESTORE":
        die("aborted by user. Nothing was changed.")
    io.write(want_of(BLOCK_A), orig)
    SLEEP(0.3)
    if io.read(want_of(BLOCK_A)) != orig:
        die("readback mismatch after restore. Keep the backup file.")
    print("Restored. Pull the monitor's POWER CABLE now, wait 10 s, plug it back in.")
    beep()
    return 0


# --------------------------------------------------------------------------- main
def edid_ok(nv, gpu, mask):
    for _ in range(6):
        e = read_edid(nv, gpu, mask)
        if e and len(e) == 128 and e[:8] == EDID_HEADER and sum(e) % 256 == 0:
            v, p = describe_edid(e)
            if (v, p) != (EDID_VENDOR, EDID_PRODUCT):
                die(f"this display is {v} {p:#06x}, not {EDID_VENDOR} {EDID_PRODUCT:#06x}. "
                    "Wrong display index? Nothing was changed.")
            print(f"EDID: {v} {p:#06x} OK")
            return
        SLEEP(0.3)
    die("could not read a valid EDID (it is flaky over NVAPI); just run again. Nothing was changed.")


def main():
    if sys.platform != "win32":
        sys.exit("This script is for Windows.")
    args = sys.argv[1:]
    display, write, restore = 0, False, None
    i = 0
    while i < len(args):
        if args[i] == "--display":
            display = int(args[i + 1]); i += 2
        elif args[i] == "--write":
            write = True; i += 1
        elif args[i] == "--restore":
            restore = args[i + 1]; i += 2
        else:
            sys.exit(__doc__)
    try:
        nv = Nv()
    except OSError as e:
        sys.exit(f"Could not load nvapi64.dll ({e}). NVIDIA GPU + driver required.")
    sel = [d for d in nv.displays() if d[0] == display]
    if not sel:
        sys.exit(f"No NVIDIA display with index {display}.")
    _, gpu, mask = sel[0]
    try:
        edid_ok(nv, gpu, mask)
        io = NvIo(nv, gpu, mask)
        if restore:
            sys.exit(cmd_restore(io, restore))
        sys.exit(cmd_write(io, os.getcwd()) if write else cmd_check(io, os.getcwd()))
    except Abort as e:
        sys.exit(f"ABORT: {e}")


if __name__ == "__main__":
    main()
