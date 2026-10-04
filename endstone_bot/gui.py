from __future__ import annotations

import json
from typing import Any

from endstone.form import ActionForm, Label, MessageForm, ModalForm, Slider, TextInput, Toggle

from endstone_bot.models import FakePlayer


class BotGUI:
    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin

    def open_main(self, player: Any) -> None:
        uuid = str(getattr(player, "unique_id", "") or "")
        name = str(getattr(player, "name", "") or "")
        owned = self._plugin.manager.bots_for_owner(uuid, name)
        _, limits = self._plugin.settings.effective(uuid, name)
        max_text = "不限" if limits.unlimited else str(limits.max_bots)
        bridge = "§a正常" if self._plugin.bridge.active else "§c离线"
        form = ActionForm(
            title="§l§b假人管理",
            content=(
                f"桥接：{bridge}§r\n"
                f"我的假人：§f{len(owned)} / §f{max_text}\n"
                f"全服假人：§f{len(self._plugin.manager.bots)} / §f{self._plugin.settings.max_total}"
            ),
        )
        form.add_button("§a创建假人\n在当前位置创建", on_click=lambda p: self.open_create(p))
        form.add_button(f"§e我的假人 ({len(owned)})\n查看、移动或删除", on_click=lambda p: self.open_my_bots(p))
        if self._plugin.is_admin(player):
            form.add_button("§c管理员面板\n全服管理、玩家限制与全局设置", on_click=lambda p: self.open_admin(p))
        player.send_form(form)

    def open_create(self, player: Any) -> None:
        form = ModalForm(
            title="§l§a创建假人",
            controls=[
                TextInput(label="假人名称", placeholder="字母/数字/_/-，最长24字符"),
                Label(text="假人会固定在创建位置；移动后可在管理菜单重新设定位置。"),
            ],
            submit_button="创建",
        )

        def submit(p: Any, result: str) -> None:
            if result is None:
                return
            try:
                data = json.loads(result)
                name = str(data[0] or "").strip()
            except Exception:
                p.send_message("§c表单数据无效。")
                return
            ok, message = self._plugin.manager.create_for_player(p, name)
            p.send_message(("§a" if ok else "§c") + message)
            if ok:
                self.open_my_bots(p)

        form.on_submit = submit
        player.send_form(form)

    def open_my_bots(self, player: Any) -> None:
        uuid = str(getattr(player, "unique_id", "") or "")
        name = str(getattr(player, "name", "") or "")
        bots = self._plugin.manager.bots_for_owner(uuid, name)
        form = ActionForm(
            title="§l§e我的假人",
            content="点击假人进行管理。" if bots else "你还没有假人。",
        )
        for fp in bots:
            status = self._plugin.manager.status_text(fp)
            form.add_button(
                f"§b{fp.name}\n{status} | {fp.dimension} | {fp.location_x:.1f}, {fp.location_y:.1f}, {fp.location_z:.1f}",
                on_click=(lambda bot=fp: lambda p: self.open_bot(p, bot, False))(),
            )
        form.add_button("§a创建假人", on_click=lambda p: self.open_create(p))
        form.add_button("返回", on_click=lambda p: self.open_main(p))
        player.send_form(form)

    def open_bot(self, player: Any, fp: FakePlayer, admin_context: bool) -> None:
        current = self._plugin.manager.get_by_name(fp.name)
        if current is None:
            player.send_message("§c这个假人已经不存在。")
            return
        fp = current
        can_manage = self._plugin.manager.can_manage(player, fp)
        form = ActionForm(
            title=f"§l§b{fp.name}",
            content=(
                f"所有者：§f{fp.owner_name}\n"
                f"状态：§f{self._plugin.manager.status_text(fp)}\n"
                f"位置：§f{fp.dimension} ({fp.location_x:.1f}, {fp.location_y:.1f}, {fp.location_z:.1f})\n"
                f"创建：§f{fp.created}"
            ),
        )
        if can_manage:
            form.add_button("§b移动到我这里\n同步当前位置和视角", on_click=lambda p: self._move_here(p, fp, admin_context))
            form.add_button("§6投掷三叉戟\n按我当前的位置和视角投掷", on_click=lambda p: self._throw_trident(p, fp, admin_context))
            form.add_button("§c删除假人", on_click=lambda p: self._confirm_remove(p, fp, admin_context))
        form.add_button(
            "返回",
            on_click=(lambda p: self.open_admin_bots(p)) if admin_context else (lambda p: self.open_my_bots(p)),
        )
        player.send_form(form)

    def _move_here(self, player: Any, fp: FakePlayer, admin_context: bool) -> None:
        ok, message = self._plugin.manager.move_here(player, fp)
        player.send_message(("§a" if ok else "§c") + message)
        self.open_bot(player, fp, admin_context)

    def _throw_trident(self, player: Any, fp: FakePlayer, admin_context: bool) -> None:
        ok, message = self._plugin.manager.throw_trident_here(player, fp)
        player.send_message(("§a" if ok else "§c") + message)
        self.open_bot(player, fp, admin_context)

    def _confirm_remove(self, player: Any, fp: FakePlayer, admin_context: bool) -> None:
        form = MessageForm(
            title=f"§l§c删除 {fp.name}",
            content=f"确定删除 §b{fp.name}§r 吗？该操作会同时断开对应 SimulatedPlayer。",
            button1="§c确认删除",
            button2="取消",
        )

        def submit(p: Any, button: int) -> None:
            if button == 0:
                ok, message = self._plugin.manager.remove(p, fp)
                p.send_message(("§a" if ok else "§c") + message)
                if admin_context:
                    self.open_admin_bots(p)
                else:
                    self.open_my_bots(p)
            else:
                self.open_bot(p, fp, admin_context)

        form.on_submit = submit
        player.send_form(form)

    def open_admin(self, player: Any) -> None:
        if not self._plugin.is_admin(player):
            player.send_message("§c只有管理员可以打开该面板。")
            return
        bridge = "§a正常" if self._plugin.bridge.active else "§c离线"
        form = ActionForm(
            title="§l§c假人管理员",
            content=f"桥接：{bridge}§r\n全服假人：§f{len(self._plugin.manager.bots)}",
        )
        form.add_button("§e全服假人\n查看和管理全部假人", on_click=lambda p: self.open_admin_bots(p))
        form.add_button("§6玩家限制\n为普通玩家调整或解除限制", on_click=lambda p: self.open_limit_players(p))
        form.add_button("§5全局设置\n调整默认数量和创建冷却", on_click=lambda p: self.open_global_settings(p))
        form.add_button("返回", on_click=lambda p: self.open_main(p))
        player.send_form(form)

    def open_admin_bots(self, player: Any) -> None:
        if not self._plugin.is_admin(player):
            return
        bots = sorted(self._plugin.manager.bots.values(), key=lambda x: (x.owner_name.lower(), x.name.lower()))
        form = ActionForm(title="§l§e全服假人", content=f"共 {len(bots)} 个假人。")
        for fp in bots:
            form.add_button(
                f"§b{fp.name}\n{fp.owner_name} | {self._plugin.manager.status_text(fp)}",
                on_click=(lambda bot=fp: lambda p: self.open_bot(p, bot, True))(),
            )
        form.add_button("返回管理员面板", on_click=lambda p: self.open_admin(p))
        player.send_form(form)

    def open_limit_players(self, player: Any) -> None:
        if not self._plugin.is_admin(player):
            return
        form = ActionForm(
            title="§l§6玩家限制",
            content="在线玩家优先显示；也可以按名称管理离线玩家。",
        )
        online = sorted(list(self._plugin.server.online_players), key=lambda p: str(p.name).lower())
        for target in online:
            target_name = str(target.name)
            key = self._plugin.settings.remember_player(str(getattr(target, "unique_id", "") or ""), target_name)
            limits = self._plugin.settings.effective_for_key(key)
            max_text = "不限" if limits.unlimited else str(limits.max_bots)
            form.add_button(
                f"§b{target_name}\n上限 {max_text} | 冷却 {limits.cooldown_seconds}s" + (" | 绕过全服" if limits.bypass_global_limit else ""),
                on_click=(lambda k=key, n=target_name: lambda p: self.open_limit_menu(p, k, n))(),
            )
        form.add_button("§e按玩家名设置\n可管理离线玩家", on_click=lambda p: self.open_limit_name_input(p))
        overrides = self._plugin.settings.configured_overrides()
        if overrides:
            form.add_button(f"§d已配置例外 ({len(overrides)})", on_click=lambda p: self.open_override_list(p))
        form.add_button("返回管理员面板", on_click=lambda p: self.open_admin(p))
        player.send_form(form)

    def open_limit_name_input(self, player: Any) -> None:
        form = ModalForm(
            title="§l§6按名称管理限制",
            controls=[TextInput(label="玩家名", placeholder="输入玩家名")],
            submit_button="继续",
        )

        def submit(p: Any, result: str) -> None:
            try:
                data = json.loads(result)
                name = str(data[0] or "").strip()
            except Exception:
                name = ""
            if not name:
                p.send_message("§c玩家名不能为空。")
                return
            key, canonical = self._plugin.settings.resolve_target_key(name, self._plugin.server.online_players)
            self.open_limit_menu(p, key, canonical)

        form.on_submit = submit
        player.send_form(form)

    def open_override_list(self, player: Any) -> None:
        form = ActionForm(title="§l§d已配置例外", content="仅显示非默认玩家限制。")
        for key, rec in self._plugin.settings.configured_overrides():
            name = str(rec.get("name", "") or key)
            limits = self._plugin.settings.effective_for_key(key)
            max_text = "不限" if limits.unlimited else str(limits.max_bots)
            form.add_button(
                f"§b{name}\n上限 {max_text} | 冷却 {limits.cooldown_seconds}s" + (" | 绕过全服" if limits.bypass_global_limit else ""),
                on_click=(lambda k=key, n=name: lambda p: self.open_limit_menu(p, k, n))(),
            )
        form.add_button("返回", on_click=lambda p: self.open_limit_players(p))
        player.send_form(form)

    def open_limit_menu(self, player: Any, key: str, name: str) -> None:
        limits = self._plugin.settings.effective_for_key(key)
        max_text = "不限" if limits.unlimited else str(limits.max_bots)
        form = ActionForm(
            title=f"§l§6{name}",
            content=(
                f"有效假人上限：§f{max_text}\n"
                f"创建冷却：§f{limits.cooldown_seconds}s\n"
                f"绕过全服上限：§f{'是' if limits.bypass_global_limit else '否'}"
            ),
        )
        form.add_button("§e编辑限制", on_click=lambda p: self.open_limit_edit(p, key, name))
        form.add_button("§a解除全部限制\n不限数量、无冷却、绕过全服上限", on_click=lambda p: self._set_unlimited(p, key, name))
        form.add_button("恢复全局默认", on_click=lambda p: self._reset_limits(p, key, name))
        form.add_button("返回", on_click=lambda p: self.open_limit_players(p))
        player.send_form(form)

    def open_limit_edit(self, player: Any, key: str, name: str) -> None:
        rec = self._plugin.settings.override(key)
        effective = self._plugin.settings.effective_for_key(key)
        unlimited = rec.get("max_bots") == -1
        no_cooldown = rec.get("cooldown_seconds") == 0
        max_default = effective.max_bots if effective.max_bots >= 0 else self._plugin.settings.max_per_player
        cooldown_default = effective.cooldown_seconds
        form = ModalForm(
            title=f"§l§6{name} - 限制",
            controls=[
                Toggle(label="解除假人数量限制", default_value=unlimited),
                Slider(label="假人上限（未解除时）", min=0, max=32, step=1, default_value=float(max(0, min(32, max_default)))),
                Toggle(label="取消创建冷却", default_value=no_cooldown),
                Slider(label="创建冷却（秒）", min=0, max=60, step=1, default_value=float(max(0, min(60, cooldown_default)))),
                Toggle(label="绕过全服假人数量上限", default_value=effective.bypass_global_limit),
            ],
            submit_button="保存",
        )

        def submit(p: Any, result: str) -> None:
            try:
                data = json.loads(result)
                is_unlimited = bool(data[0])
                max_bots = int(float(data[1]))
                is_no_cd = bool(data[2])
                cooldown = int(float(data[3]))
                bypass = bool(data[4])
            except Exception:
                p.send_message("§c表单数据无效。")
                return
            self._plugin.settings.set_override(
                key,
                name=name,
                max_bots=-1 if is_unlimited else max_bots,
                cooldown_seconds=0 if is_no_cd else cooldown,
                bypass_global_limit=bypass,
            )
            p.send_message(f"§a已更新 {name} 的假人限制。")
            self.open_limit_menu(p, key, name)

        form.on_submit = submit
        player.send_form(form)

    def _set_unlimited(self, player: Any, key: str, name: str) -> None:
        self._plugin.settings.set_unlimited(key, name)
        player.send_message(f"§a已解除 {name} 的全部假人限制。")
        self.open_limit_menu(player, key, name)

    def _reset_limits(self, player: Any, key: str, name: str) -> None:
        self._plugin.settings.reset_override(key, name)
        player.send_message(f"§a{name} 已恢复全局默认限制。")
        self.open_limit_menu(player, key, name)

    def open_global_settings(self, player: Any) -> None:
        form = ModalForm(
            title="§l§5全局假人设置",
            controls=[
                Slider(label="全服默认总上限", min=1, max=32, step=1, default_value=float(min(32, self._plugin.settings.max_total))),
                Slider(label="普通玩家默认上限", min=0, max=8, step=1, default_value=float(min(8, self._plugin.settings.max_per_player))),
                Slider(label="默认创建冷却（秒）", min=0, max=60, step=1, default_value=float(min(60, self._plugin.settings.spawn_cooldown_seconds))),
                Toggle(label="启用挂机位置守护", default_value=self._plugin.settings.guard_enabled),
                Slider(label="位置守护距离（方块）", min=0.5, max=8.0, step=0.5, default_value=float(max(0.5, min(8.0, self._plugin.settings.guard_distance)))),
            ],
            submit_button="保存",
        )

        def submit(p: Any, result: str) -> None:
            try:
                data = json.loads(result)
                total = int(float(data[0]))
                per = int(float(data[1]))
                cooldown = int(float(data[2]))
                guard = bool(data[3])
                distance = float(data[4])
            except Exception:
                p.send_message("§c表单数据无效。")
                return
            self._plugin.settings.set_global("maxtotal", total)
            self._plugin.settings.set_global("maxperplayer", per)
            self._plugin.settings.set_global("cooldown", cooldown)
            self._plugin.settings.set_guard(enabled=guard, distance=distance)
            p.send_message("§a全局假人设置已保存并立即生效。")
            self.open_admin(p)

        form.on_submit = submit
        player.send_form(form)
