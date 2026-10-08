#include <string.h>

#include "pico/unique_id.h"
#include "tusb.h"

// The IR Toy's IDs, so the kernel's ir_toy driver binds and cdc_acm leaves
// the device alone.
#define USB_VID 0x04d8
#define USB_PID 0xfd08

#define EP_NOTIF 0x81
#define EP_OUT 0x02
#define EP_IN 0x82

enum { STR_LANGID, STR_MANUFACTURER, STR_PRODUCT, STR_SERIAL, STR_CDC };

static const tusb_desc_device_t device = {
    .bLength = sizeof(tusb_desc_device_t),
    .bDescriptorType = TUSB_DESC_DEVICE,
    .bcdUSB = 0x0200,
    .bDeviceClass = TUSB_CLASS_MISC,
    .bDeviceSubClass = MISC_SUBCLASS_COMMON,
    .bDeviceProtocol = MISC_PROTOCOL_IAD,
    .bMaxPacketSize0 = CFG_TUD_ENDPOINT0_SIZE,
    .idVendor = USB_VID,
    .idProduct = USB_PID,
    .bcdDevice = 0x0100,
    .iManufacturer = STR_MANUFACTURER,
    .iProduct = STR_PRODUCT,
    .iSerialNumber = STR_SERIAL,
    .bNumConfigurations = 1,
};

#define CONFIG_LEN (TUD_CONFIG_DESC_LEN + TUD_CDC_DESC_LEN)

static const uint8_t configuration[] = {
    TUD_CONFIG_DESCRIPTOR(1, 2, 0, CONFIG_LEN, 0, 100),
    TUD_CDC_DESCRIPTOR(0, STR_CDC, EP_NOTIF, 8, EP_OUT, EP_IN, 64),
};

static const char *const strings[] = {
    [STR_MANUFACTURER] = "fire-blaster",
    [STR_PRODUCT] = "IR blaster (IR Toy compatible)",
    [STR_CDC] = "IR Toy",
};

const uint8_t *tud_descriptor_device_cb(void)
{
    return (const uint8_t *)&device;
}

const uint8_t *tud_descriptor_configuration_cb(uint8_t index)
{
    (void)index;
    return configuration;
}

const uint16_t *tud_descriptor_string_cb(uint8_t index, uint16_t langid)
{
    (void)langid;
    static uint16_t desc[1 + 32];
    char serial[2 * PICO_UNIQUE_BOARD_ID_SIZE_BYTES + 1];
    const char *s;
    size_t len;

    if (index == STR_LANGID) {
        desc[1] = 0x0409;
        len = 1;
    } else {
        if (index == STR_SERIAL) {
            pico_get_unique_board_id_string(serial, sizeof serial);
            s = serial;
        } else if (index < sizeof strings / sizeof strings[0] && strings[index]) {
            s = strings[index];
        } else {
            return NULL;
        }
        len = strlen(s);
        if (len > 32)
            len = 32;
        for (size_t i = 0; i < len; i++)
            desc[1 + i] = (uint8_t)s[i];
    }
    desc[0] = (uint16_t)(TUSB_DESC_STRING << 8 | (2 * len + 2));
    return desc;
}
