# Pico IR blaster firmware

Firmware for a Raspberry Pi Pico (or Pico 2) that drives an IR LED. Over USB
the Pico presents itself as a USB IR Toy, so the Linux `ir_toy` driver (in
mainline since 5.10) binds it and shows it as `/dev/lircN`. The host needs
no extra software: fireblasterd, `ir-ctl` and anything else that talks LIRC
can use it.

Only transmit is supported. The kernel also registers a receiver for the
device, but it never reports any IR.

## Wiring

An IR transmitter module with GND/VCC/DAT pins, where DAT feeds the LED
through a resistor (active high):

| Module | Pico pin    |
|--------|-------------|
| GND    | 38 (GND)    |
| DAT    | 4 (GP2)     |
| VCC    | unconnected |

GP2 drives the LED directly at 12 mA, which gives a range of a metre or two.
For more range, switch the LED through a transistor from VBUS. The firmware
stays the same. To use another pin, pass `-DIR_TX_PIN=<gpio>` at configure
time. Never put 5 V on a GPIO.

The Pico's onboard LED lights while a code is being sent (not on the Pico W
or Pico 2 W, whose LED hangs off the wireless chip).

### Waveshare RP2040-One

The [RP2040-One](https://www.waveshare.com/wiki/RP2040-One) has a PCB USB-A
plug, so it fits straight into the host. On its left edge, counting from the
plug, are 5V, GND, 3V3 and GP29. The module fits onto GND, 3V3 and GP29 with
no wires:

1. Pull the module's middle header pin (VCC). Otherwise it would sit in the
   3V3 hole.
2. Solder module GND into the GND hole and module DAT into GP29.
3. Optionally trim the module's PCB edge so it lines up with the shoulder of
   the USB plug. There are no traces there on the AliExpress module
   ([this one](https://www.aliexpress.us/item/3256808454565276.html)).

Build with:

```sh
cmake -B build -DPICO_BOARD=waveshare_rp2040_one -DIR_TX_PIN=29
```

Its RGB LED is a WS2812 on GP16, which this firmware doesn't drive. The
module's red indicator flashes on each send instead.

## Building

You need CMake, an `arm-none-eabi` GCC toolchain and the Pico SDK 2.x. On
Arch: `pacman -S cmake arm-none-eabi-gcc arm-none-eabi-newlib`.

```sh
cd firmware/pico
cmake -B build -DPICO_SDK_PATH=/path/to/pico-sdk   # or -DPICO_SDK_FETCH_FROM_GIT=on
cmake --build build
```

The default target is the Pico 2. For the original Pico, add
`-DPICO_BOARD=pico`. The images are not interchangeable.

To flash, hold BOOTSEL while plugging the Pico in, then copy
`build/fire_blaster_pico.uf2` to the drive that appears.

Protocol tests run on the host: `make -C test`.

## Checking it

```sh
journalctl -k | grep ir_toy     # "version: hardware 2, firmware 2.2, protocol 1"
ir-ctl -d /dev/lirc0 --features
ir-ctl -d /dev/lirc0 -S nec:0x04
```

Most phone front cameras show the LED flashing.

## How it works

- `src/irtoy.c` speaks the IR Toy's sampling-mode protocol, as far as the
  kernel driver uses it:
  - reset, version (`V222`) and sample mode (`S01`);
  - carrier (`0x06`), sent as the PIC's PWM period;
  - transmit (`0x03`): a stream of big-endian 21 µs counts, paced by a
    free-space handshake, with a byte count (`t`) and `C` after it.

  Durations are buffered until the `0xffff` terminator, then sent as one
  transmission.
- `src/ir_tx.pio` makes the carrier: 12 state machine cycles per carrier
  period, high for 4 (33% duty). Marks and spaces are whole carrier
  periods.
- The USB IDs are the IR Toy's (`04d8:fd08`). The `ir_toy` driver matches
  them, and `cdc_acm` is told to ignore them. These IDs belong to Dangerous
  Prototypes, so this is for personal use only.
