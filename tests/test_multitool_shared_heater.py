"""Hardware-free regression tests: python -m unittest discover -s tests -v."""

import importlib.util
import pathlib
import threading
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_module(name):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / 'klipper' / 'extras' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


multitool = load_module('multitool')
filament = load_module('multitool_filament')


class CommandError(Exception):
    pass


class Reactor:
    def __init__(self):
        self.now = 0.

    def monotonic(self):
        return self.now

    def pause(self, until):
        self.now = until
        return self.now

    def register_timer(self, *args):
        pass

    def register_callback(self, *args):
        pass


class Heater:
    min_temp, max_temp = 0., 320.

    def __init__(self, printer):
        self.printer = printer
        self.lock = threading.Lock()
        self.target_temp = 0.
        self.temperature = 220.
        self.raw_temperature = None
        self.off_latency = 0.
        self.off_at = 0.
        self.sensor_frozen = False

    @property
    def last_temp_time(self):
        return 0. if self.sensor_frozen else int(self.printer.reactor.now * 10) / 10.

    @property
    def last_temp(self):
        return (self.temperature if self.raw_temperature is None
                else self.raw_temperature)

    def set_temp(self, target):
        if target and not self.min_temp <= target <= self.max_temp:
            raise CommandError('Heater temperature out of range')
        self.target_temp = target
        self.printer.events.append(('target', target))
        if target == 0:
            self.off_at = self.printer.reactor.now + self.off_latency

    def get_pwm_delay(self):
        return 0.3

    def get_status(self, eventtime):
        power = 0.5 if self.target_temp > 0 or eventtime < self.off_at else 0.
        return {'target': self.target_temp,
                'temperature': self.temperature, 'power': power}


class Extruder:
    def __init__(self, heater):
        self.heater = heater

    def get_heater(self):
        return self.heater

    def get_status(self, eventtime):
        return self.heater.get_status(eventtime)


class Heaters:
    def set_temperature(self, heater, target, wait=False):
        heater.set_temp(target)


class Toolhead:
    max_accel = 5000.

    def __init__(self, printer):
        self.printer = printer

    def get_last_move_time(self):
        return self.printer.reactor.now

    def get_status(self, eventtime):
        return {'homed_axes': 'xyz', 'max_velocity': 200.,
                'max_accel': self.max_accel, 'square_corner_velocity': 5.}


class GCode:
    def __init__(self, printer):
        self.printer = printer
        self.commands = []
        self.hooks = {}
        self.fail_command = None
        self.gcode_handlers = {
            'MULTITOOL_RELEASE_TOOL': self.release,
            'MULTITOOL_PICKUP_TOOL': self.pickup,
            'TURN_OFF_HEATERS': self.turn_off,
        }

    def register_command(self, name, handler, **kwargs):
        old = self.gcode_handlers.pop(name, None)
        if handler is not None:
            self.gcode_handlers[name] = handler
        return old

    def respond_info(self, message):
        pass

    def release(self, gcmd):
        self.printer.events.append(('release', gcmd.get_int('TOOL')))
        if 'release' in self.hooks:
            self.hooks['release']()

    def pickup(self, gcmd):
        self.printer.events.append(('pickup', gcmd.get_int('TOOL')))
        if 'pickup' in self.hooks:
            self.hooks['pickup']()

    def turn_off(self, gcmd):
        for obj in self.printer.objects.values():
            if isinstance(obj, Extruder):
                obj.get_heater().set_temp(0.)

    def run_script(self, script):
        for line in script.splitlines():
            self.run_script_from_command(line)

    def run_script_from_command(self, command):
        self.commands.append(command)
        self.printer.events.append(('command', command))
        if self.fail_command and command.startswith(self.fail_command):
            raise CommandError('Injected failure: ' + command)
        words = command.split()
        name = words[0].upper()
        params = dict(w.split('=', 1) for w in words[1:] if '=' in w)
        if name in self.gcode_handlers:
            self.gcode_handlers[name](GCmd(params))
        elif name == 'G4':
            self.printer.reactor.now += float(words[1][1:]) / 1000.
        elif name == 'M99104':
            tool = next(int(w[1:]) for w in words[1:] if w.startswith('T'))
            target = next(float(w[1:]) for w in words[1:] if w.startswith('S'))
            section = 'extruder' if tool == 0 else 'extruder%d' % tool
            self.printer.lookup_object(section).get_heater().set_temp(target)
        elif name == 'M104':
            tool = next(int(w[1:]) for w in words[1:] if w.startswith('T'))
            target = next(float(w[1:]) for w in words[1:] if w.startswith('S'))
            self.printer.objects['multitool']._set_tool_temperature(tool, target)
        elif name == 'TEMPERATURE_WAIT':
            heater = self.printer.lookup_object(params['SENSOR']).get_heater()
            # Model the native wait reaching its smoothed-temperature window.
            heater.temperature = (float(params['MINIMUM'])
                                  + float(params['MAXIMUM'])) / 2.


class GCmd:
    def __init__(self, params=None):
        self.params = params or {}

    def get(self, key, default=None):
        return self.params.get(key, default)

    def get_int(self, key, default=None, **kwargs):
        return int(self.get(key, default))

    def get_float(self, key, default=None, **kwargs):
        return float(self.get(key, default))

    def respond_info(self, message):
        pass


class Printer:
    command_error = config_error = CommandError

    def __init__(self, current_tool=0):
        self.events = []
        self.reactor = Reactor()
        self.objects = {'heaters': Heaters(), 'toolhead': Toolhead(self)}
        self.objects['gcode'] = GCode(self)
        self.heater = Heater(self)
        self.objects['extruder'] = Extruder(self.heater)
        self.objects['save_variables'] = type('SaveVariables', (), {
            'allVariables': {'current_tool': current_tool}})()

    def lookup_object(self, name, *default):
        if name in self.objects:
            return self.objects[name]
        if default:
            return default[0]
        raise CommandError('Unknown object: ' + name)

    def get_reactor(self):
        return self.reactor

    def register_event_handler(self, *args):
        pass

    def is_shutdown(self):
        return False


class Config:
    def __init__(self, printer, **values):
        self.printer = printer
        self.values = {'tool_count': 3, 'sync_active_spool': False,
                       'default_pressure_advance_extruder': ''}
        self.values.update(values)

    def get_printer(self):
        return self.printer

    def get(self, key, default=None):
        return self.values.get(key, default)

    def getint(self, key, default=None, **kwargs):
        return int(self.get(key, default))

    def getfloat(self, key, default=None, **kwargs):
        return float(self.get(key, default))

    def getboolean(self, key, default=None):
        return bool(self.get(key, default))

    def getchoice(self, key, choices, default=None):
        value = self.get(key, default)
        if value not in choices:
            raise CommandError('Invalid choice: ' + str(value))
        return choices[value] if isinstance(choices, dict) else value

    def has_section(self, section):
        return section in self.printer.objects

    def getsection(self, section):
        return self


class SharedHeaterTests(unittest.TestCase):
    def make_plugin(self, mode='shared', current_tool=0, **values):
        printer = Printer(current_tool)
        name = values.get('shared_extruder', 'extruder')
        if mode == 'shared' and name != 'extruder':
            printer.objects[name] = printer.objects.pop('extruder')
        plugin = multitool.Multitool(Config(printer, heater_mode=mode, **values))
        printer.objects['multitool'] = plugin
        plugin._on_connect()
        plugin._on_ready()
        return plugin, printer

    def test_shared_tools_map_to_one_extruder(self):
        plugin, _ = self.make_plugin()
        self.assertEqual(plugin._tool_extruder_name(1), 'extruder')
        self.assertEqual(plugin._tool_extruder_name(2), 'extruder')

    def test_inactive_target_is_cached_without_heating_active_tool(self):
        plugin, printer = self.make_plugin()
        plugin._set_tool_temperature(0, 220.)
        plugin._set_tool_temperature(1, 200.)
        self.assertEqual(printer.heater.target_temp, 220.)
        self.assertEqual(plugin._heater_target(1), 200.)

    def test_mode_is_opt_in(self):
        printer = Printer()
        plugin = multitool.Multitool(Config(printer))
        self.assertEqual(plugin.heater_mode, 'per_tool')
        self.assertEqual(plugin._tool_extruder_name(1), 'extruder1')

    def test_independent_mode_keeps_separate_heaters_and_raw_command(self):
        plugin, printer = self.make_plugin('per_tool')
        other = Heater(printer)
        printer.objects['extruder1'] = Extruder(other)
        plugin._set_tool_temperature(0, 220.)
        plugin._set_tool_temperature(1, 200.)
        self.assertEqual(printer.heater.target_temp, 220.)
        self.assertEqual(other.target_temp, 200.)
        self.assertIn('M99104 T1 S200.000000', printer.objects['gcode'].commands)
        plugin._do_change_tool(GCmd(), 1)
        self.assertEqual(printer.heater.target_temp, 220.)
        self.assertEqual(other.target_temp, 200.)
        self.assertIn('ACTIVATE_EXTRUDER EXTRUDER=extruder1',
                      printer.objects['gcode'].commands)

    def test_custom_shared_extruder_is_used_for_motion_and_wait(self):
        plugin, printer = self.make_plugin(shared_extruder='extruder2')
        plugin._set_tool_temperature(0, 220.)
        plugin._do_change_tool(GCmd(), 1)
        commands = printer.objects['gcode'].commands
        self.assertEqual(plugin.extruder_motion_sync_stepper, 'extruder2')
        self.assertEqual(plugin._tool_extruder_name(1), 'extruder2')
        self.assertIn('ACTIVATE_EXTRUDER EXTRUDER=extruder2', commands)
        self.assertTrue(any(c.startswith('TEMPERATURE_WAIT SENSOR=extruder2 ')
                            for c in commands))

    def test_missing_shared_extruder_fails_at_connect(self):
        printer = Printer()
        plugin = multitool.Multitool(Config(
            printer, heater_mode='shared', shared_extruder='missing'))
        with self.assertRaisesRegex(CommandError, 'configured extruder'):
            plugin._on_connect()

    def test_swap_disables_before_motion_and_heats_after_pickup(self):
        plugin, printer = self.make_plugin(extrude_compensation_length=1.)
        plugin._set_tool_temperature(0, 220.)
        plugin._set_tool_temperature(1, 200.)
        printer.events.clear()
        gcode = printer.objects['gcode']

        def check_off():
            self.assertEqual(printer.heater.target_temp, 0.)
            self.assertEqual(printer.heater.get_status(printer.reactor.now)['power'], 0.)

        gcode.hooks.update(release=check_off, pickup=check_off)
        plugin._do_change_tool(GCmd(), 1)
        events = printer.events
        self.assertLess(events.index(('target', 0.)), events.index(('release', 0)))
        self.assertLess(events.index(('release', 0)), events.index(('pickup', 1)))
        self.assertLess(events.index(('pickup', 1)), events.index(('target', 200.)))
        retract = next(i for i, e in enumerate(events)
                       if e[0] == 'command' and e[1].startswith('G1 E-1.'))
        prime = next(i for i, e in enumerate(events)
                     if e[0] == 'command' and e[1].startswith('G1 E1.'))
        wait = next(i for i, e in enumerate(events)
                    if e[0] == 'command' and e[1].startswith('TEMPERATURE_WAIT '))
        self.assertLess(retract, events.index(('target', 0.)))
        self.assertLess(wait, prime)
        self.assertEqual(plugin.current_tool, 1)
        self.assertEqual(plugin._heater_target(0), 220.)
        self.assertEqual(printer.heater.target_temp, 200.)

    def test_unset_target_inherits_but_explicit_zero_does_not(self):
        for explicit_zero in (False, True):
            with self.subTest(explicit_zero=explicit_zero):
                plugin, printer = self.make_plugin()
                plugin._set_tool_temperature(0, 220.)
                if explicit_zero:
                    plugin._set_tool_temperature(1, 0.)
                plugin._do_change_tool(GCmd(), 1)
                self.assertEqual(printer.heater.target_temp,
                                 0. if explicit_zero else 220.)

    def test_unload_keeps_heater_off_and_blocks_direct_heating(self):
        plugin, printer = self.make_plugin()
        plugin._set_tool_temperature(0, 220.)
        plugin._do_change_tool(GCmd(), -1)
        self.assertEqual(plugin.current_tool, -1)
        self.assertEqual(printer.heater.target_temp, 0.)
        with self.assertRaises(CommandError):
            printer.heater.set_temp(200.)
        plugin._set_tool_temperature(1, 210.)
        self.assertEqual(printer.heater.target_temp, 0.)
        plugin._do_change_tool(GCmd(), 1)
        self.assertEqual(printer.heater.target_temp, 210.)

    def test_no_tool_preheat_only_caches_target(self):
        plugin, printer = self.make_plugin(current_tool=-1)
        plugin.cmd_MULTITOOL_SET_TEMPERATURE(GCmd({'TOOL': 1, 'S': 200}))
        self.assertEqual(printer.heater.target_temp, 0.)
        with self.assertRaises(CommandError):
            plugin.cmd_MULTITOOL_WAIT_TEMPERATURE(GCmd({'TOOL': 1, 'S': 200}))
        plugin._do_change_tool(GCmd(), 1)
        self.assertEqual(printer.heater.target_temp, 200.)

    def test_inactive_wait_fails_without_heating_or_moving(self):
        plugin, printer = self.make_plugin()
        plugin._set_tool_temperature(0, 220.)
        printer.events.clear()
        with self.assertRaises(CommandError):
            plugin._wait_temperature(1, 200.)
        self.assertEqual(printer.heater.target_temp, 220.)
        self.assertEqual(printer.events, [])

    def test_direct_heater_api_updates_active_logical_target(self):
        plugin, printer = self.make_plugin()
        printer.heater.set_temp(215.)
        self.assertEqual(plugin._heater_target(0), 215.)
        plugin._do_change_tool(GCmd(), 1)
        self.assertEqual(printer.heater.target_temp, 215.)

    def test_direct_heating_during_pickup_is_blocked(self):
        plugin, printer = self.make_plugin()
        plugin._set_tool_temperature(0, 220.)
        printer.objects['gcode'].hooks['pickup'] = lambda: printer.heater.set_temp(200.)
        with self.assertRaises(CommandError):
            plugin._do_change_tool(GCmd(), 1)
        self.assertEqual(printer.heater.target_temp, 0.)
        self.assertTrue(plugin._shared_heater_fault)

    def test_off_waits_for_reported_pwm_and_scheduling_delay(self):
        plugin, printer = self.make_plugin()
        plugin._set_tool_temperature(0, 220.)
        printer.heater.off_latency = 0.2
        times = []
        printer.objects['gcode'].hooks['release'] = lambda: times.append(printer.reactor.now)
        plugin._do_change_tool(GCmd(), 1)
        self.assertGreaterEqual(times[0], 0.2 + printer.heater.get_pwm_delay() + 0.1)

    def test_off_timeout_blocks_all_release_and_pickup_motion(self):
        plugin, printer = self.make_plugin(shared_heater_off_timeout=0.1)
        plugin._set_tool_temperature(0, 220.)
        printer.heater.off_latency = 10.
        with self.assertRaisesRegex(CommandError, 'Timed out'):
            plugin._do_change_tool(GCmd(), 1)
        self.assertFalse(any(e[0] in ('release', 'pickup') for e in printer.events))
        self.assertEqual(printer.heater.target_temp, 0.)
        self.assertTrue(plugin._shared_heater_fault)

    def test_stale_or_invalid_sensor_cannot_reenable_heating(self):
        for failure in ('stale', 'nan', 'above_max'):
            with self.subTest(failure=failure):
                plugin, printer = self.make_plugin(shared_heater_sensor_timeout=0.2)
                plugin._set_tool_temperature(0, 220.)
                if failure == 'stale':
                    printer.heater.sensor_frozen = True
                else:
                    printer.heater.temperature = (float('nan')
                                                  if failure == 'nan' else 400.)
                with self.assertRaisesRegex(CommandError, 'valid shared temperature'):
                    plugin._do_change_tool(GCmd(), 1)
                self.assertEqual(printer.heater.target_temp, 0.)
                self.assertTrue(plugin._shared_heater_fault)

    def test_fresh_sensor_sample_is_required_after_pickup(self):
        plugin, printer = self.make_plugin()
        plugin._set_tool_temperature(0, 220.)
        pickup_samples = []
        printer.objects['gcode'].hooks['pickup'] = lambda: pickup_samples.append(
            printer.heater.last_temp_time)
        plugin._do_change_tool(GCmd(), 1)
        self.assertGreater(printer.heater.last_temp_time, pickup_samples[0])
        self.assertEqual(printer.heater.target_temp, 220.)

    def test_invalid_raw_sample_cannot_be_hidden_by_valid_smoothed_temperature(self):
        for raw in (float('nan'), 400.):
            with self.subTest(raw=raw):
                plugin, printer = self.make_plugin(shared_heater_sensor_timeout=0.2)
                plugin._set_tool_temperature(0, 220.)
                printer.heater.raw_temperature = raw
                with self.assertRaisesRegex(CommandError, 'valid shared temperature'):
                    plugin._do_change_tool(GCmd(), 1)
                self.assertEqual(printer.heater.target_temp, 0.)
                self.assertTrue(plugin._shared_heater_fault)

    def test_wait_requires_new_tool_raw_temperature_before_priming(self):
        plugin, printer = self.make_plugin(extrude_compensation_length=1.)
        plugin._set_tool_temperature(0, 220.)
        plugin._set_tool_temperature(1, 200.)
        printer.heater.raw_temperature = 25.
        pause = printer.reactor.pause

        def warm_new_tool(until):
            result = pause(until)
            if any(c.startswith('TEMPERATURE_WAIT ')
                   for c in printer.objects['gcode'].commands):
                printer.heater.raw_temperature = 200.
                printer.events.append(('raw_ready', 200.))
            return result

        printer.reactor.pause = warm_new_tool
        plugin._do_change_tool(GCmd(), 1)
        prime = next(i for i, event in enumerate(printer.events)
                     if event[0] == 'command' and event[1].startswith('G1 E1.'))
        self.assertIn(('raw_ready', 200.), printer.events)
        self.assertLess(printer.events.index(('raw_ready', 200.)), prime)

    def test_explicit_wait_also_checks_raw_temperature(self):
        plugin, printer = self.make_plugin()
        plugin._set_tool_temperature(0, 200.)
        printer.heater.raw_temperature = 25.
        pause = printer.reactor.pause

        def warm_tool(until):
            result = pause(until)
            printer.heater.raw_temperature = 200.
            return result

        printer.reactor.pause = warm_tool
        plugin.cmd_MULTITOOL_WAIT_TEMPERATURE(GCmd({'TOOL': 0, 'S': 200}))
        self.assertGreater(printer.reactor.now, 0.)
        self.assertEqual(printer.heater.last_temp, 200.)

    def test_shutdown_during_raw_temperature_wait_locks_heating(self):
        plugin, printer = self.make_plugin()
        plugin._set_tool_temperature(0, 220.)
        printer.heater.raw_temperature = 25.
        pause = printer.reactor.pause

        def shut_down_while_heating(until):
            result = pause(until)
            if any(c.startswith('TEMPERATURE_WAIT ')
                   for c in printer.objects['gcode'].commands):
                printer.is_shutdown = lambda: True
            return result

        printer.reactor.pause = shut_down_while_heating
        with self.assertRaisesRegex(CommandError, 'shared heater temperature'):
            plugin._do_change_tool(GCmd(), 1)
        self.assertEqual(printer.heater.target_temp, 0.)
        self.assertTrue(plugin._shared_heater_fault)

    def test_shutdown_during_sensor_wait_keeps_heating_disabled(self):
        plugin, printer = self.make_plugin()
        plugin._set_tool_temperature(0, 220.)

        def shut_down():
            printer.is_shutdown = lambda: True

        printer.objects['gcode'].hooks['pickup'] = shut_down
        with self.assertRaisesRegex(CommandError, 'shared temperature sensor'):
            plugin._do_change_tool(GCmd(), 1)
        self.assertEqual(printer.heater.target_temp, 0.)
        self.assertTrue(plugin._shared_heater_fault)

    def test_continuation_group_replacement_uses_shared_logical_target(self):
        plugin, printer = self.make_plugin()
        plugin._set_tool_temperature(0, 220.)
        plugin._set_tool_temperature(1, 200.)
        runout = filament.MultitoolFilament.__new__(filament.MultitoolFilament)
        runout.multitool = plugin
        runout.printer = printer
        runout.gcode = printer.objects['gcode']
        runout.tool_count = 3
        runout._loaded = [True, False, True]
        runout._group_of = {1: [1, 2], 2: [1, 2]}
        printer.objects['multitool_filament'] = runout
        plugin._do_change_tool(GCmd(), 1)
        self.assertEqual(plugin.current_tool, 2)
        self.assertEqual(plugin._heater_target(2), 200.)
        self.assertEqual(printer.heater.target_temp, 200.)

    def test_unhomed_unload_preparation_failure_also_disables_heating(self):
        plugin, printer = self.make_plugin()
        plugin._set_tool_temperature(0, 220.)
        printer.objects['toolhead'].get_status = lambda eventtime: {'homed_axes': ''}
        printer.objects['gcode'].fail_command = 'G28 '
        with self.assertRaises(CommandError):
            plugin.cmd_UNTOOL(GCmd())
        self.assertEqual(printer.heater.target_temp, 0.)
        self.assertTrue(plugin._shared_heater_fault)
        self.assertFalse(any(e[0] in ('release', 'pickup') for e in printer.events))

    def test_clamp_and_stats_errors_also_disable_heating(self):
        for failure in ('entry', 'released', 'clamped', 'commit'):
            with self.subTest(failure=failure):
                plugin, printer = self.make_plugin()
                plugin._set_tool_temperature(0, 220.)

                def check(state, reason=''):
                    if ((failure == 'entry' and reason == '入口校验')
                            or (failure == state and reason != '入口校验')):
                        raise CommandError('Injected clamp failure')

                def commit():
                    if failure == 'commit':
                        raise CommandError('Injected stats failure')

                printer.objects['multitool_clamp'] = type('Clamp', (), {
                    'assert_state': staticmethod(check)})()
                printer.objects['multitool_stats'] = type('Stats', (), {
                    'tc_begin': staticmethod(lambda: None),
                    'stage_begin': staticmethod(lambda stage: None),
                    'stage_end': staticmethod(lambda stage: None),
                    'tc_commit': staticmethod(commit),
                    'tc_abort': staticmethod(lambda: None)})()
                with self.assertRaises(CommandError):
                    plugin._do_change_tool(GCmd(), 1)
                self.assertEqual(printer.heater.target_temp, 0.)
                self.assertFalse(plugin.active)

    def test_shutdown_during_off_wait_blocks_mechanical_hooks(self):
        plugin, printer = self.make_plugin()
        plugin._set_tool_temperature(0, 220.)
        printer.is_shutdown = lambda: printer.reactor.now > 0.
        with self.assertRaises(CommandError):
            plugin._do_change_tool(GCmd(), 1)
        self.assertFalse(any(e[0] in ('release', 'pickup') for e in printer.events))
        self.assertEqual(printer.heater.target_temp, 0.)

    def test_swap_failures_leave_heating_locked_even_if_cleanup_fails(self):
        for failure in ('release', 'pickup', 'TEMPERATURE_WAIT ',
                        'RESTORE_GCODE_STATE ', 'offset'):
            with self.subTest(failure=failure):
                plugin, printer = self.make_plugin()
                plugin._set_tool_temperature(0, 220.)
                gcode = printer.objects['gcode']

                def fail():
                    raise CommandError('Injected failure')

                if failure in ('release', 'pickup'):
                    gcode.hooks[failure] = fail
                elif failure == 'offset':
                    printer.objects['multitool_offsets'] = type('Offsets', (), {
                        'apply': staticmethod(lambda *a, **kw: fail())})()
                else:
                    gcode.fail_command = failure
                with self.assertRaises(CommandError):
                    plugin._do_change_tool(GCmd(), 1)
                self.assertEqual(printer.heater.target_temp, 0.)
                self.assertTrue(plugin._shared_heater_fault)
                self.assertFalse(plugin.active)
                self.assertEqual(plugin.change_to_tool, -1)
                with self.assertRaises(CommandError):
                    printer.heater.set_temp(220.)

    def test_same_tool_cannot_bypass_fault_but_verified_pickup_can_recover(self):
        plugin, printer = self.make_plugin()
        plugin._set_tool_temperature(0, 220.)
        printer.objects['gcode'].fail_command = 'MULTITOOL_RELEASE_TOOL '
        with self.assertRaises(CommandError):
            plugin._do_change_tool(GCmd(), 1)
        with self.assertRaises(CommandError):
            plugin._do_change_tool(GCmd(), 0)
        with self.assertRaises(CommandError):
            plugin._set_tool_temperature(0, 220.)
        printer.objects['gcode'].fail_command = None
        plugin._do_change_tool(GCmd(), -1)
        plugin._do_change_tool(GCmd(), 1)
        self.assertFalse(plugin._shared_heater_fault)
        self.assertEqual(printer.heater.target_temp, 220.)

    def test_global_off_clears_all_targets_and_prevents_reheat(self):
        plugin, printer = self.make_plugin()
        plugin._set_tool_temperature(0, 220.)
        plugin._set_tool_temperature(1, 200.)
        printer.objects['gcode'].run_script_from_command('TURN_OFF_HEATERS')
        plugin._do_change_tool(GCmd(), 1)
        self.assertEqual(printer.heater.target_temp, 0.)
        self.assertEqual(plugin.get_status(0)['tool_target_temperatures'], [0., 0., 0.])

    def test_external_off_during_pickup_cancels_queued_reheat(self):
        for command in ('direct', 'TURN_OFF_HEATERS'):
            with self.subTest(command=command):
                plugin, printer = self.make_plugin()
                plugin._set_tool_temperature(0, 220.)
                plugin._set_tool_temperature(1, 200.)
                gcode = printer.objects['gcode']
                gcode.hooks['pickup'] = (
                    lambda: printer.heater.set_temp(0.) if command == 'direct'
                    else gcode.run_script_from_command(command))
                plugin._do_change_tool(GCmd(), 1)
                self.assertEqual(printer.heater.target_temp, 0.)

    def test_invalid_cached_temperatures_are_rejected_without_changing_state(self):
        plugin, printer = self.make_plugin()
        plugin._set_tool_temperature(0, 220.)
        for target in (-1., 321., float('nan'), float('inf')):
            with self.subTest(target=target), self.assertRaises(CommandError):
                plugin._set_tool_temperature(1, target)
        self.assertEqual(plugin._heater_target(1), 0.)
        self.assertEqual(printer.heater.target_temp, 220.)

    def test_restart_restores_identity_without_reheating(self):
        plugin, printer = self.make_plugin(current_tool=2)
        self.assertEqual(plugin.current_tool, 2)
        self.assertEqual(printer.heater.target_temp, 0.)
        self.assertEqual(plugin.get_status(0)['tool_target_temperatures'], [0., 0., 0.])

    def test_status_reports_only_the_observed_mounted_temperature(self):
        plugin, printer = self.make_plugin()
        plugin._set_tool_temperature(0, 220.)
        plugin._set_tool_temperature(1, 200.)
        status = plugin.get_status(0)
        self.assertEqual(status['heater_mode'], 'shared')
        self.assertEqual(status['tool_extruders'], ['extruder'] * 3)
        self.assertEqual(status['tool_temperatures'], [220., None, None])
        self.assertEqual(status['tool_target_temperatures'], [220., 200., 0.])
        plugin._do_change_tool(GCmd(), -1)
        self.assertEqual(plugin.get_status(0)['tool_temperatures'], [None] * 3)

    def test_runout_continuation_does_not_turn_off_new_shared_tool(self):
        plugin, printer = self.make_plugin()
        plugin._set_tool_temperature(0, 220.)
        runout = filament.MultitoolFilament.__new__(filament.MultitoolFilament)
        runout.multitool = plugin
        runout.printer = printer
        runout.gcode = printer.objects['gcode']
        runout.reactor = printer.reactor
        runout._loaded = [False, True, True]
        runout._is_printing = lambda eventtime: True
        runout._find_next_loaded = lambda tool: 1
        runout.runout_event_delay = 1.
        runout._handling_runout = True
        self.assertEqual(runout._heater_target(0), 220.)
        runout._runout_event_handler(0.)
        self.assertEqual(plugin.current_tool, 1)
        self.assertEqual(plugin._heater_target(0), 0.)
        self.assertEqual(printer.heater.target_temp, 220.)
        self.assertIn('RESUME', runout.gcode.commands)


if __name__ == '__main__':
    unittest.main()
