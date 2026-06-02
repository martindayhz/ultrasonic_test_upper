import unittest

from protocol import (
    CMD_MASTER_READ,
    CMD_MASTER_WRITE,
    CMD_SLAVE_READ_RESPONSE,
    CMD_SLAVE_WRITE_RESPONSE,
    build_meter_number_request,
    build_meter_read_frame,
    build_meter_write_frame,
    build_passthrough_frame,
    bytes_to_hex,
    hex_to_bytes,
    parse_meter_frame,
    parse_meter_number_response,
    parse_passthrough_response,
)
from registers import decode_register_value, encode_register_value, load_registers
from serial_win32 import SerialConfig


class ProtocolTests(unittest.TestCase):
    def test_meter_number_request_is_fixed_frame(self):
        self.assertEqual(
            bytes_to_hex(build_meter_number_request()),
            "68 10 AA AA AA AA AA AA AA 03 03 0A 81 02 B1 16",
        )

    def test_meter_number_response_extracts_seven_byte_number(self):
        response = "FF 68 10 00 29 15 24 03 26 20 83 03 0A 81 02 36 16"
        parsed = parse_meter_number_response(response)
        self.assertEqual(bytes_to_hex(parsed.meter_number), "00 29 15 24 03 26 20")
        self.assertTrue(parsed.checksum_ok)

    def test_read_register_0001_uses_master_read_01_and_checksum_b3(self):
        frame = build_meter_read_frame(0x0001)
        self.assertEqual(bytes_to_hex(frame), "68 07 07 68 53 FE 51 0F 01 00 01 B3 16")
        parsed = parse_meter_frame(frame)
        self.assertEqual(parsed.command, CMD_MASTER_READ)
        self.assertEqual(parsed.register_address, 0x0001)
        self.assertTrue(parsed.checksum_ok)

    def test_read_response_requires_81_and_decodes_dn15(self):
        regs = load_registers("__missing_registers.json")
        frame = parse_meter_frame("68 09 09 68 08 FE 78 0F 81 00 01 00 0F 1E 16", CMD_SLAVE_READ_RESPONSE)
        self.assertEqual(frame.command, CMD_SLAVE_READ_RESPONSE)
        self.assertEqual(frame.register_address, 0x0001)
        self.assertIn("DN15", decode_register_value(regs[0x0001], frame.data))

    def test_write_register_0001_uses_master_write_02(self):
        regs = load_registers("__missing_registers.json")
        payload = encode_register_value(regs[0x0001], "DN15")
        frame = build_meter_write_frame(0x0001, payload)
        parsed = parse_meter_frame(frame)
        self.assertEqual(parsed.command, CMD_MASTER_WRITE)
        self.assertEqual(parsed.register_address, 0x0001)
        self.assertEqual(bytes_to_hex(payload), "00 0F")
        self.assertEqual(frame[-2], 0xC3)

    def test_write_response_requires_82(self):
        frame = parse_meter_frame("68 07 07 68 08 FE 78 0F 82 00 01 10 16", CMD_SLAVE_WRITE_RESPONSE)
        self.assertEqual(frame.command, CMD_SLAVE_WRITE_RESPONSE)
        self.assertEqual(frame.register_address, 0x0001)
        self.assertTrue(frame.checksum_ok)
        self.assertEqual(frame.data, b"")

    def test_document_write_response_sample_checksum_warning_does_not_block_parse(self):
        frame = parse_meter_frame("68 07 07 68 08 FE 78 0F 82 00 01 DA 16", CMD_SLAVE_WRITE_RESPONSE)
        self.assertFalse(frame.checksum_ok)
        self.assertEqual(frame.expected_checksum, 0x10)
        self.assertEqual(frame.command, CMD_SLAVE_WRITE_RESPONSE)

    def test_document_write_sample_checksum_warning_does_not_block_parse(self):
        frame = parse_meter_frame("68 09 09 68 53 FE 51 0F 02 00 01 00 0F 06 16", CMD_MASTER_WRITE)
        self.assertFalse(frame.checksum_ok)
        self.assertEqual(frame.expected_checksum, 0xC3)
        self.assertEqual(frame.command, CMD_MASTER_WRITE)

    def test_passthrough_request_matches_document_outer_length_and_checksum(self):
        meter_no = hex_to_bytes("90 01 00 01 26 11 13")
        meter_frame = build_meter_read_frame(0x0001)
        wrapped = build_passthrough_frame(meter_frame, meter_no, sequence=1)
        self.assertEqual(wrapped[10], 0x13)
        self.assertEqual(wrapped[-2], 0xA8)
        self.assertEqual(
            bytes_to_hex(wrapped),
            "68 10 90 01 00 01 26 11 13 04 13 BB 19 01 01 0D 00 "
            "68 07 07 68 53 FE 51 0F 01 00 01 B3 16 A8 16",
        )

    def test_passthrough_response_unwraps_fe_prefixed_meter_frame(self):
        response = (
            "FF 68 10 90 01 00 01 26 11 13 84 18 19 BB 01 01 12 00 "
            "FE FE FE 68 09 09 68 08 FE 78 0F 81 00 01 00 0F 1E 16 06 16"
        )
        outer = parse_passthrough_response(response)
        self.assertTrue(outer.checksum_ok)
        self.assertEqual(outer.passthrough_length, 18)
        frame = parse_meter_frame(outer.payload, CMD_SLAVE_READ_RESPONSE)
        self.assertEqual(frame.command, CMD_SLAVE_READ_RESPONSE)
        self.assertEqual(bytes_to_hex(frame.data), "00 0F")

    def test_default_serial_config_uses_even_parity(self):
        self.assertEqual(SerialConfig(port="COM1").parity, "E")


if __name__ == "__main__":
    unittest.main()
