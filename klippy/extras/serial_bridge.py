# Support for "serial bridge"
#
# Copyright (C) 2019-2020  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging

POSTFIX = b'\xff\xff\xff'
QUERY_TIME = 0.2

class PrinterSerialBridge:
    def __init__(self, config):
        self.log("Init") 
        self.callbacks = []       
        self.printer = config.get_printer()
        self.name = config.get_name()
        self.reactor = self.printer.get_reactor()
        self.printer.register_event_handler("klippy:ready", self.handle_ready)

        self.gcode = self.printer.lookup_object("gcode")
        self.gcode.register_command("SERIAL_BRIDGE_SEND", self.cmd_SERIAL_BRIDGE_SEND)

        self.gcode = self.printer.lookup_object("gcode")
        self.gcode.register_command("SERIAL_BRIDGE_STATS", self.cmd_SERIAL_BRIDGE_STATS)

        self.gcode.register_output_handler(self._output_callback)

        ppins = self.printer.lookup_object("pins")
        pin_params = ppins.lookup_pin(config.get("tx_pin"))
        rx_pin_params = ppins.lookup_pin(config.get("rx_pin"))
        self.mcu = pin_params['chip']
        self.oid = self.mcu.create_oid()
        self.mcu.register_config_callback(self.build_config)
        self.gcode = self.printer.lookup_object('gcode')

        self.input_buffer = ""

    def register_callback(self, callback):
        self.callbacks.append(callback)

    def _output_callback(self, msg):
        self.log("GCODE OUTPUT: " + msg)
        self.send_text(msg)

    def cmd_SERIAL_BRIDGE_SEND(self, gcmd):
        self.log("SERIAL_BRIDGE_SEND: " + gcmd.get("TEXT"))
        self.log("SERIAL_BRIDGE_SEND: " + bytes(gcmd.get("TEXT"), 'utf-8').hex())
        self.send_text(gcmd.get("TEXT"))

    def cmd_SERIAL_BRIDGE_STATS(self, gcmd):
        self.serial_bridge_stats_cmd.send()
        self.log("SERIAL_BRIDGE_STATS" + gcmd.get("TEXT"))

    def chunkstring(self, string, length):
        return (string[0+i:length+i] for i in range(0, len(string), length))

    def send_text(self, text):
        delimiterpos = text.find("//")
        if delimiterpos >= 0:
            striped_text = text[:delimiterpos]
        else:
            striped_text = text
        if (len(striped_text) == 0):
            print("SERIAL_BRIDGE_SEND IGNORE: " + text)
            return

        chunks = self.chunkstring(striped_text + "\n", 40)
        self.send_serial(bytes(text, encoding='utf-8'))
        #for chunk in chunks:
        #    self.serial_bridge_send_cmd.send([self.oid, bytes(chunk, encoding='utf-8')])
        #    print("SERIAL_BRIDGE_SEND: " + chunk)

    def send_serial(self, msg):
        self.serial_bridge_send_cmd.send([self.oid, msg + POSTFIX])

    def build_config(self):
        rest_ticks = self.mcu.seconds_to_clock(QUERY_TIME)
        clock = self.mcu.get_query_slot(self.oid)
        self.mcu.add_config_cmd("command_config_serial_bridge oid=%d clock=%d rest_ticks=%d" % (self.oid, clock, rest_ticks))

        cmd_queue = self.mcu.alloc_command_queue()

        self.mcu.register_response(self._handle_serial_bridge_response, "serial_bridge_response")
        self.serial_bridge_send_cmd = self.mcu.lookup_command(
            "serial_bridge_send oid=%c text=%*s",
            cq=cmd_queue)        

    def _handle_serial_bridge_response(self, params):
        data = params["text"]
        byte_debug = ' '.join(['0x{:02x}'.format(byte) for byte in data])
        self.log("Response: " + byte_debug)

        for callback in self.callbacks:
            callback(data)

    def process_message(self, message):
        self.log("Process message: " + str(message))        
        return self.reactor.NEVER
        
    def _process_input_buffer(self):
        delimiterpos = self.input_buffer.rfind("\n")
        while delimiterpos > 0:
            commands = self.input_buffer[:delimiterpos]
            self.gcode.respond_info(commands, log=False)
            self.gcode._process_commands(commands.split("\n"))
            self.input_buffer = self.input_buffer[delimiterpos + 1:]
            delimiterpos = self.input_buffer.rfind("\n")

    def handle_ready(self):
        self.log("ready")        

    def log(self, msg, *args, **kwargs):
        logging.info("SERIAL BRIDGE: " + str(msg))

def load_config_prefix(config):
    return PrinterSerialBridge(config)
