# 共享加热与测温模式

## 适用结构

该模式面向多个可更换的被动热端，共用同一个物理挤出机、加热输出和温度传感器的机器。
例如固定感应加热线圈和固定红外测温传感器，只更换线圈中的热端。
实现不依赖感应加热或 MLX90614 的专用命令，也可用于其他由 Klipper heater API 管理的共享温控系统。

默认 `heater_mode: per_tool` 保持原来的映射：T0 → `extruder`，T1 → `extruder1`，以此类推。
`heater_mode: shared` 将所有逻辑工具映射到 `shared_extruder`，同时保留各工具的偏移、料盘、耗材检测和统计。
不通过重复配置同一组引脚来创建虚假的独立加热器或温度传感器。

## 配置和安装

先保持现有单热端的挤出、加热和测温配置正常工作。在现有的 `[multitool]` 中设置：

```ini
[multitool]
tool_count: 4
heater_mode: shared
shared_extruder: extruder
shared_heater_off_timeout: 5.0
shared_heater_sensor_timeout: 5.0
```

以上是需要合并的配置项，不要再添加一个重复的 `[multitool]`。

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `heater_mode` | `per_tool` | `per_tool` 或 `shared`，新增模式需要明确启用 |
| `shared_extruder` | `extruder` | 共享模式使用的现有物理 extruder 名称 |
| `shared_heater_off_timeout` | `5.0` | 等待 Klipper 上报零 PWM 的超时秒数，必须大于零 |
| `shared_heater_sensor_timeout` | `5.0` | 抓取后等待新、有效温度采样的超时秒数，必须大于零 |

共享模式中，`extruder_motion_sync_stepper` 和 `default_pressure_advance_extruder` 的默认值跟随
`shared_extruder`。如有额外的挤出步进安排，仍可明确设置原有参数。

安装源码可以使用仓库中的仅 Klipper 插件脚本：

```bash
bash ~/klipper-toolchange-stats/install_klipper_plugins.sh
```

该脚本会将仓库中的 Python extras 软链接到 Klipper；会替换同名目标，安装前应检查现有插件。
它不生成配置、不自动启用插件，也不重启服务。共享硬件应手动合并配置，
不要使用完整安装向导生成的每工具独立 `heater_pin` / `sensor_pin` 模板。
配置入口、`[save_variables]`、释放/抓取宏等仍按项目原有要求配置。
如需 Orca lane_data 的 Moonraker 组件，应按原有说明另行安装和启用。

配置中应只保留一份 `M104` / `M109` 覆写，使用 `multitool_config.cfg` 中调用
`MULTITOOL_SET_TEMPERATURE` / `MULTITOOL_WAIT_TEMPERATURE` 的实现。
已有自定义 `M104` / `M109` 的机器需要合并，不能重复声明同名宏。
完成配置、静态检查和停机安排后再重启 Klipper。

## 换头顺序

1. 沿用原有入口检查、状态保存、抬升及偏移处理。
2. 如配置了挤出补偿，在旧热端仍挂载且温度允许时完成回抽。
3. 等待已有运动结束，记住旧热端的目标温度，锁住共享加热并将物理目标置零。
4. 等待 Klipper 上报零 PWM，再等待 PWM 调度延迟过去；超时则禁止释放和抓取。
5. 调用 `multitool_release_tool` 放回旧热端，完成运动及可选的夹紧状态检查。
6. 调用 `multitool_pickup_tool` 抓取新热端，完成运动及可选的夹紧状态检查。
7. 等待共享传感器取得晚于抓取完成时间的有效温度采样。
8. 恢复新热端的目标温度，按原有逻辑等温，再执行挤出补偿和后续偏移应用。

共享模式在恢复加热前检查新采样的原始温度；等温时要求原始温度和平滑温度
都进入目标 ±1.5°C 的范围，避免旧热端残留的平滑读数使新热端提前通过等温。

释放和抓取钩子在本模式中只负责机械动作及必要的就位验证，不应加热或自行 prime。
普通 heater API 在此阶段的非零目标请求会报错；通过 `M104` 设置的逻辑目标只会缓存。
如需清线/排料，应放在新热端完成温度等待之后。

入口、运动、钩子、夹紧、采样、等温、状态恢复或偏移应用任一阶段失败，都会将共享加热目标置零并锁定加热。
`shared_heater_fault` 状态用于标识此情况；重复选择同一个工具不能绕过锁定。
先检查实际工具位置和机构状态，再完成一次正确的卸下、重新抓取流程，才能恢复加热。
这是显式的故障恢复动作，不应使用未知机构状态执行自动重试。

## 温度命令语义

- `M104 Tn Sxxx`：记录 Tn 的目标温度。只有它是当前已挂载、未锁定的工具时，才驱动物理加热器。
- 无工具挂载时可先用 `M104` 缓存温度，再执行 `Tn` 抓取；缓存本身不会加热。
- `M109 Tn Sxxx`：应先成功选择 Tn，再等待温度。等待未挂载工具会报错，不会隐式换头。
- 某工具第一次抓取、且从未明确设置过温度时，继承上一挂载工具的目标温度。
- 明确设置过 `S0` 的工具不会继承温度；`UNTOOL` 关闭物理加热，但保留逻辑目标，供后续抓取使用。
- `TURN_OFF_HEATERS` 同时清空所有逻辑目标，避免取消打印后换头又从缓存恢复加热。
- 换头期间通过正常 heater API 明确关闭加热，也会取消已缓存的恢复目标。
- 前端直接发送的 `SET_HEATER_TEMPERATURE` 等正常 heater API 请求同样受挂载/换头锁保护。
- 重启只沿用原有的工具身份恢复机制，不恢复加热目标。
- 断料续打读取主模块的逻辑目标；换头后关闭旧工具只清空旧工具目标，不会误关新工具的共享加热。

## 前端状态

`printer.multitool` 新增以下状态字段，旧字段保留：

- `heater_mode`、`shared_extruder`：温控模式及物理对象。
- `shared_heater_suspended`、`shared_heater_fault`：换头/无工具锁和故障锁。
- `tool_extruders`：工具到物理 extruder 的映射数组。
- `tool_target_temperatures`：每个逻辑工具的目标温度数组。
- `tool_temperatures`：各工具的实测温度。共享模式只提供当前已挂载且未挂起工具的读数，其他项为 `null`。

停靠热端不在固定传感器视野内，不应把共享传感器的当前读数显示成所有热端的实测值。
仍将 Tn 写死映射到 `extruderN` 的旧前端需要使用上述映射和逻辑目标字段进行适配；
本仓库的后端改动不会自动修改独立发布的 Mainsail 前端产物。

## 测试及硬件边界

运行无需连接打印机的回归测试：

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖独立温控模式、共享映射、温度缓存、关闭与恢复顺序、PWM 超时、
抓取后传感器新采样、原始/平滑温度分离、全局关热、故障恢复及断料续打组替代。

零 PWM 来自 Klipper 的软件状态，并非线圈电流、MOSFET 或硬件 EN 的电气反馈。
共享驱动仍须正确实现 Klipper PWM 调度、关闭输出和硬件故障保护。
绕过普通 heater API 的直接功率测试、PID 校准控制器或驱动专用维护命令不属于本加热锁的完整保护范围；
不能在机械换头期间调用这些命令，也不能把本模式当作独立的硬件联锁。

软件测试不代表实机验证。首次部署应先验证关热时序和冷态机械流程，再验证就位检测、
红外读数、升温及打印恢复。若机器已有降低或关闭温控故障检测的设置，需要单独核对，
不能依赖本插件补偿温控驱动自身的保护缺失。
