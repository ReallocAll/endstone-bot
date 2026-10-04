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
- Behavior Pack 通信使用启动期随机 token + protocol version，并限制为 Server 来源。
- 不自动修改 `level.dat`。找不到精确世界目录时 fail closed，不猜测其他世界。

## 默认限制

```json
{
  "limits": {
    "max_total": 6,
    "max_per_player": 1,
    "spawn_cooldown_seconds": 10
  },
  "position_guard": {
    "enabled": true,
    "interval_ticks": 10,
    "distance": 1.0
  }
}
```

配置文件位于插件数据目录的 `config.json`。管理员 GUI 和 `/bot config ...` 修改后立即生效。

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

插件只根据 `server.level.name` 精确寻找当前世界，自动安装/升级内置 Behavior Pack，并更新 `world_behavior_packs.json`。它**不会自动修改 `level.dat`**。

SimulatedPlayer 依赖 Beta APIs。如果行为包桥接未建立，日志会提示检查完整重启和 Beta APIs。首次安装或 Behavior Pack 版本升级后需要完整重启 BDS。

## Bridge protocol 2

- Python → Behavior Pack 使用 `/scriptevent`；Behavior Pack → Python 使用内部 `/botbridge` 命令回调，payload 采用 UTF-8 hex（仅 `0-9a-f`），避免命令参数对 `%`、引号或空格的词法限制，并避免依赖 Script API 自发 `scriptevent` 是否再次进入 Endstone 的 `ScriptMessageEvent` hook。\n- `bot:hello` 建立随机 token；后续消息必须使用同一 token。
- 优先接受 `sourceType=Server`；兼容 Endstone `ConsoleCommandSender` 产生的无实体/无方块/NPC 来源命令，同时拒绝玩家、实体、命令方块和 NPC 来源。
- `/reload` 时使用经过认证的 `bot:shutdown` 清理远端 SimulatedPlayer 并释放旧 token。
- heartbeat 超时后插件真正进入断开状态。
- 坐标和列表按消息长度分批，避免撞 `/scriptevent` 2048 字符上限。
- 优先使用模块级 `spawnSimulatedPlayer(DimensionLocation, ...)`；若当前 BDS 没有该接口，则回退到长生命周期 GameTest，并生成后传送到目标绝对世界坐标和维度。

## 数据

- `bots.json`：持久化假人定义和挂机锚点。
- `config.json`：全局资源限制。
- `player_limits.json`：玩家级例外。

写入均采用临时文件 + replace。

## 上游与许可证

本 fork 基于 BCZZB/endstone-bot 与 mcbes-manage-script by YueHua46。见仓库 `LICENSE` 和 Required Notice。
