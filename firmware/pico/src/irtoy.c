#include "irtoy.h"

// Commands, from the IR Toy sampling mode docs and the kernel driver.
#define CMD_RESET 0x00         // leave sample mode
#define CMD_TX_START 0x03
#define CMD_SET_CARRIER 0x06   // followed by PR2 and a duty byte
#define CMD_TX_COUNT 0x24      // report byte count after transmit
#define CMD_TX_NOTIFY 0x25     // report 'C' when done
#define CMD_TX_HANDSHAKE 0x26  // report free buffer space per packet
#define CMD_VERSION 'v'
#define CMD_SAMPLE_MODE 's'

// Free space claimed in each handshake. Durations are buffered whole, so
// this only has to fit in one 64-byte packet; 62 is what the IR Toy sends.
#define HANDSHAKE_SPACE 62

static const uint8_t REPLY_VERSION[] = { 'V', '2', '2', '2' };  // hardware 2, firmware 22
static const uint8_t REPLY_SAMPLE_MODE[] = { 'S', '0', '1' };   // protocol 1

void irtoy_init(irtoy_t *t, const irtoy_io_t *io)
{
    t->io = io;
    t->state = IRTOY_COMMAND;
    t->carrier_hz = irtoy_carrier_hz(78);  // ~38 kHz until told otherwise
    t->carrier_len = 0;
    t->count = 0;
    t->tx_bytes = 0;
    t->high_byte = -1;
    t->overflow = false;
}

static void send(irtoy_t *t, const uint8_t *buf, size_t len)
{
    t->io->send(t->io->ctx, buf, len);
}

static void handshake(irtoy_t *t)
{
    uint8_t space = HANDSHAKE_SPACE;
    send(t, &space, 1);
}

static void tx_start(irtoy_t *t)
{
    t->state = IRTOY_TX_DATA;
    t->count = 0;
    t->tx_bytes = 0;
    t->high_byte = -1;
    t->overflow = false;
    handshake(t);
}

static void tx_finish(irtoy_t *t)
{
    if (!t->overflow && t->count)
        t->io->transmit(t->io->ctx, t->units, t->count, t->carrier_hz);

    uint8_t count[] = { 't', t->tx_bytes >> 8, t->tx_bytes & 0xff };
    send(t, count, sizeof count);
    uint8_t done = t->overflow ? 'F' : 'C';
    send(t, &done, 1);
    t->state = IRTOY_SAMPLE;
}

// Returns true when the 0xffff terminator was read.
static bool tx_byte(irtoy_t *t, uint8_t b)
{
    t->tx_bytes++;
    if (t->high_byte < 0) {
        t->high_byte = b;
        return false;
    }
    uint16_t v = (uint16_t)(t->high_byte << 8 | b);
    t->high_byte = -1;
    if (v == 0xffff)
        return true;
    if (t->count < IRTOY_MAX_DURATIONS)
        t->units[t->count++] = v;
    else
        t->overflow = true;
    return false;
}

static void command_byte(irtoy_t *t, uint8_t b)
{
    switch (b) {
    case CMD_RESET:
        t->state = IRTOY_COMMAND;
        break;
    case CMD_VERSION:
        send(t, REPLY_VERSION, sizeof REPLY_VERSION);
        break;
    case CMD_SAMPLE_MODE:
        t->state = IRTOY_SAMPLE;
        send(t, REPLY_SAMPLE_MODE, sizeof REPLY_SAMPLE_MODE);
        break;
    case CMD_SET_CARRIER:
        if (t->state == IRTOY_SAMPLE) {
            t->state = IRTOY_CARRIER;
            t->carrier_len = 0;
        }
        break;
    case CMD_TX_START:
        if (t->state == IRTOY_SAMPLE)
            tx_start(t);
        break;
    default:
        // 0x24-0x26 select which transmit replies to send; the driver
        // always asks for all of them, so they are always sent. Anything
        // else (0xff from the reset sequence) is ignored.
        break;
    }
}

void irtoy_feed(irtoy_t *t, const uint8_t *buf, size_t len)
{
    bool tx_data = false;

    for (size_t i = 0; i < len; i++) {
        uint8_t b = buf[i];
        switch (t->state) {
        case IRTOY_TX_DATA:
            tx_data = true;
            if (tx_byte(t, b)) {
                tx_data = false;
                tx_finish(t);
            }
            break;
        case IRTOY_CARRIER:
            t->carrier_arg[t->carrier_len++] = b;
            if (t->carrier_len == 2) {
                // The second byte is a duty cycle the driver leaves at 0;
                // duty is fixed by the PIO program.
                t->carrier_hz = irtoy_carrier_hz(t->carrier_arg[0]);
                t->state = IRTOY_SAMPLE;
            }
            break;
        default:
            command_byte(t, b);
            break;
        }
    }

    // The driver sends the next chunk of durations only after a handshake.
    if (tx_data && t->state == IRTOY_TX_DATA)
        handshake(t);
}
