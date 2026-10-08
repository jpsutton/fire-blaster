// USB IR Toy v2 protocol, transmit side only, as spoken by the Linux ir_toy
// driver (drivers/media/rc/ir_toy.c). Kept free of SDK calls so it can be
// tested on the host.

#ifndef IRTOY_H
#define IRTOY_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

// Duration of one count in the transmit stream. The real IR Toy ticks at
// 21.33 us, but the kernel converts with 21, so 21 reproduces its durations.
#define IRTOY_UNIT_US 21

// The kernel sends at most 1024 durations per write (LIRCBUF_SIZE).
#define IRTOY_MAX_DURATIONS 2048

typedef struct {
    // Send one USB packet. Replies must arrive as separate packets: the
    // driver tells them apart by length.
    void (*send)(void *ctx, const uint8_t *buf, size_t len);
    // Emit durations (in IRTOY_UNIT_US counts, pulse first, alternating)
    // and return when the last one is out.
    void (*transmit)(void *ctx, const uint16_t *units, size_t count, uint32_t carrier_hz);
    void *ctx;
} irtoy_io_t;

typedef enum {
    IRTOY_COMMAND,   // after reset: 'v', 's'
    IRTOY_SAMPLE,    // sample mode: carrier, transmit start
    IRTOY_CARRIER,   // reading the two bytes after 0x06
    IRTOY_TX_DATA,   // reading big-endian durations up to 0xffff
} irtoy_state_t;

typedef struct {
    const irtoy_io_t *io;
    irtoy_state_t state;
    uint32_t carrier_hz;
    uint8_t carrier_arg[2];
    uint8_t carrier_len;
    uint16_t units[IRTOY_MAX_DURATIONS];
    size_t count;
    uint16_t tx_bytes;     // reported back in the 't' reply
    int high_byte;         // first byte of a duration, or -1
    bool overflow;
} irtoy_t;

void irtoy_init(irtoy_t *t, const irtoy_io_t *io);

// Feed the bytes of one OUT packet.
void irtoy_feed(irtoy_t *t, const uint8_t *buf, size_t len);

// Carrier for a PR2 value from the 0x06 command. The IR Toy's PIC runs
// PWM from a 48 MHz clock with a 1:16 prescaler.
static inline uint32_t irtoy_carrier_hz(uint8_t pr2)
{
    return 48000000u / (16u * ((uint32_t)pr2 + 1));
}

#endif
