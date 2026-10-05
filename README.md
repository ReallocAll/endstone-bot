# endstone-bot (server fork)

面向 Endstone/BDS 生存服务器的轻量 **SimulatedPlayer 假人管理插件**。本 fork 基于 BCZZB/endstone-bot，保留 GameTest SimulatedPlayer 桥接，删除与挂机假人无关的 AI、NPC/entity、皮肤和 practice 遗留路径，并补上生产服务器需要的资源限制、管理员例外、GUI 与控制台管理。

> 许可证仍为 PolyForm Noncommercial License 1.0.0。本 fork 面向非商业/公益服务器使用；上游 Required Notice 与许可证文件必须保留。

## 设计目标

- 只使用真正的 `SimulatedPlayer`，不再提供 NPC + tickingarea 伪假人。
- 普通玩家默认受严格资源限制，避免无限创建造成 DoS。
- 管理员可以给指定玩家单独提高上限，或一键解除全部限制，适合生电玩家/技术玩家。
- `/bot` 直接打开玩家 GUI，适合 ClockMenu 只执行一个命令接入。
- 管理员 GUI 可以管理全服假人、玩家例外和全局限制。
- 控制台可以完成状态、列表、创建、移动、删除、玩家限制和全局设置，不依赖 GUI。
- “移动到我这里”负责保存假人的位置和世界空间视线方向；“投掷三叉戟”只使用这个已保存姿态，不会再次把假人传送到操作者身上。三叉戟实现采用与 FlashFakePlayerPack 等同类 Bedrock 假人行为包一致的原生 SimulatedPlayer 路径：假人由长期 GameTest 的 `Test.spawnSimulatedPlayer` 创建，背包内已有三叉戟会临时换到快捷栏 0，执行 `useItemInSlot(0)`，10 tick 后 `stopUsingItem()`。“移动到我这里”会先移动到保存的位置/维度，再以假人头部为起点，沿保存的 `dx/dy/dz` 计算世界绝对目标；由于 Test-bound SimulatedPlayer 的控制器位置参数使用 GameTest 相对坐标，插件会通过 `activeTest.relativeLocation(worldTarget)` 转换后再调用 `lookAtLocation(..., LookDuration.UntilMove)` 同步完整 pitch/yaw。该朝向会保持到假人发生下一次移动；投掷阶段不会再次修改视角。投掷阶段不会再次移动或修改视角，只执行原生 `useItemInSlot(0)` / `stopUsingItem()`。投掷前如果假人当前实际位置 1.0 格欧氏距离内有其他玩家则拒绝。不会生成或补充物品，也不自行构造 projectile。
- 假人管理菜单新增“整理背包”。开始前插件会把玩家和假人的主背包完整 ItemStack/NBT 快照写入事务日志，然后清空并下线假人，把假人的主背包临时交给玩家整理；盔甲和副手不会参与同步。整理完成后，把希望假人拿着的物品拿在手上，再使用 `/bot` 重新打开假人菜单并选择“完成背包整理”。归还后假人会拿着你完成整理时手上拿着的物品，同时玩家原来的主背包会恢复。整个过程使用持久化事务状态和 digest 校验，任何歧义都会 fail-close，而不是复制物品。
- 假人可以手动上线/下线；该状态会持久化。手动下线后不会被定时补生或 bridge reconcile 自动拉回。假人管理中的实际操作完成后 GUI 会关闭，玩家可移动、整理背包或调整站位后再手动重新打开菜单继续下一步。
- 假人死亡后会通过 `SimulatedPlayer.respawn()` 自动重生，并恢复保存的挂机锚点和视角；若原对象无法复活，则回退到重新创建流程。
- Behavior Pack 通信使用启动期随机 token + protocol version，并限制为 Server 来源。
- 插件不会在运行中的服务器里自动修改 `level.dat`。它会在 `plugins/bot/` 生成一个明确的离线补丁脚本，停服后用当前 Python 解释器执行即可启用 Beta APIs。

## 默认限制

```json
{
  "limits": {
    "max_total": 10,
    "max_per_player": 0,
    "spawn_cooldown_seconds": 10
  },
  "position_guard": {
    "enabled": true,
    "interval_ticks": 10,
    "distance": 1.0
  }
}
```

配置文件位于插件数据目录的 `config.json`。默认普通玩家不能创建假人（上限 0），全服总上限为 10；管理员可以通过配置文件、游戏内 [管理员面板 → 全局设置] 或 `/bot config maxtotal|maxperplayer|cooldown` 调整，全局设置修改后立即生效。

玩家例外保存在 `player_limits.json`：

- `max_bots = -1`：不限假人数量。
- `cooldown_seconds = 0`：取消创建冷却。
- `bypass_global_limit = true`：该玩家可以绕过全服总上限。

管理员 GUI 中的“解除全部限制”会一次设置这三项。

## GUI / ClockMenu

玩家执行 `/bot` 即可打开主 GUI。ClockMenu 推荐直接把菜单按钮绑定到 `/bot`。

管理员也可以在 GUI 中进入“管理员面板”，或执行 `/bot admin`。

## 命令

普通玩家：

```text
/bot
/bot gui
/bot status
/bot list
/bot spawn <name>
/bot remove <name>
/bot tp <name>
/bot trident <name>
/bot online <name>
/bot offline <name>
/bot inventory <name> start
/bot inventory <name> done
```

管理员/控制台：

```text
/bot createat <name> <owner> <x> <y> <z> <overworld|nether|the_end>
/bot moveat <name> <x> <y> <z> <overworld|nether|the_end>
/bot remove <name>
/bot removeall

/bot limit <player> show
/bot limit <player> unlimited
/bot limit <player> default
/bot limit <player> max <count>
/bot limit <player> cooldown <seconds>
/bot limit <player> bypassglobal <true|false>

/bot config show
/bot config reload
/bot config maxtotal <count>
/bot config maxperplayer <count>
/bot config cooldown <seconds>
```

给生电玩家解除全部限制：

```text
/bot limit Alice unlimited
```

恢复默认：

```text
/bot limit Alice default
```

离线玩家也可以先按名字配置。玩家下一次上线后，插件会把名称例外迁移到 UUID，并把该玩家旧的 name-only 假人所有权绑定到 UUID。

## Behavior Pack / Beta APIs

SimulatedPlayer 依赖 Beta APIs。插件**不会自动修改正在运行的世界**，也不再使用 `on_load`、`atexit`、后台 helper 子进程等机制。

插件启用时会根据已经加载的 `server.level.name` 精确定位当前世界，并在插件数据目录生成：

```text
plugins/bot/enable_beta.py
```

脚本中写入的是当前世界 `level.dat` 的精确绝对路径，并且脚本本身是**单文件、自包含、仅依赖 Python 标准库**的，不需要 `endstone_bot` 能被当前解释器 import，也不需要设置 `PYTHONPATH`。需要启用 Beta APIs 时：

1. 正常执行 `stop`，等待 BDS 完全退出；
2. 使用运行 Endstone 的同一个 Python 环境执行脚本；
3. 再启动服务器。

Windows：

```powershell
python .\plugins\bot\enable_beta.py
```

Linux：

```bash
python plugins/bot/enable_beta.py
```

这个脚本只在你显式执行时修改文件，不会被 `/reload`、插件启停或进程退出自动触发。

安全写入包含：

- 使用插件运行时确认过的当前世界精确 `level.dat` 路径，不扫描或猜测其他世界；
- 拒绝符号链接、NBT 长度头异常、重复字段和错误字段类型；
- 完整解析修改前后的 little-endian NBT；
- 只允许 `experiments.gametest`、`experiments_ever_used`、`saved_with_toggled_experiments` 发生语义变化；
- 修改前在 `plugins/bot/level_dat_backups/<world>/` 创建并校验 SHA-256 备份；
- 同目录临时文件 + `fsync` + `os.replace` 原子替换；
- 写后重新解析验证，失败自动回滚。

如果世界已经启用 Beta APIs，再次执行脚本只会报告已启用，不会重写 `level.dat`。

Behavior Pack 根据已经加载的 `server.level.name` 自动安装/升级并更新 `world_behavior_packs.json`。首次安装或 Pack 版本升级后仍需要完整重启一次。

## Bridge protocol 2

- Python → Behavior Pack 使用 `/scriptevent`；Behavior Pack → Python 使用内部 `/botbridge` 命令回调，payload 采用 UTF-8 hex（仅 `0-9a-f`），避免命令参数对 `%`、引号或空格的词法限制，并避免依赖 Script API 自发 `scriptevent` 是否再次进入 Endstone 的 `ScriptMessageEvent` hook。
- `bot:hello` 建立随机 token；后续消息必须使用同一 token。
- 优先接受 `sourceType=Server`；兼容 Endstone `ConsoleCommandSender` 产生的无实体/无方块/NPC 来源命令，同时拒绝玩家、实体、命令方块和 NPC 来源。
- `/reload` 时使用经过认证的 `bot:shutdown` 清理远端 SimulatedPlayer 并释放旧 token。
- heartbeat 超时后插件真正进入断开状态。
- 坐标和列表按消息长度分批，避免撞 `/scriptevent` 2048 字符上限。
- SimulatedPlayer 统一由长生命周期 GameTest 的 `Test.spawnSimulatedPlayer` 创建；GameTest 所需的 1×1×1 空结构由 Script API 在运行时创建并保存到 World，不再打包静态 `.mcstructure`。

## 数据

- `bots.json`：持久化假人定义、挂机锚点、pitch/yaw 和世界空间视线方向。
- `config.json`：全局资源限制与位置守护设置。
- `enable_beta.py`：插件自动生成的显式离线 Beta APIs 补丁脚本。
- `level_dat_backups/<world>/`：手动执行补丁脚本时创建的校验备份。
- `player_limits.json`：玩家级例外。

写入均采用临时文件 + replace。

## 上游与许可证

本 fork 基于 BCZZB/endstone-bot 与 mcbes-manage-script by YueHua46。见仓库 `LICENSE` 和 Required Notice。
