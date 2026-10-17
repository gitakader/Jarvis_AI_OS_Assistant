"""
ui_router.py — Smart Provider Router & Fallback Management Overlay for MARK LV HUD.

Provides an interactive cyberpunk-styled UI modal with 3 dedicated sections:
1. Provider Register: Register and configure providers, endpoints, and models.
2. API Key Setup: Multi-key pooling, rotation strategy, and key masking.
3. Fallback Order: Interactive visual ladder reordering for Chat, Vision, Fast, and Search.
"""
from __future__ import annotations

import json
import threading
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QScrollArea, QTextEdit, QVBoxLayout, QWidget,
    QListWidget, QListWidgetItem
)

from core.model_router import router, DEFAULT_PROVIDERS


class ModelRouterOverlay(QWidget):
    """Floating overlay for Provider Registry, Multi-Key Pool, and Fallback Ladders."""

    _test_done = pyqtSignal(bool, str)
    _ladder_test_done = pyqtSignal(str)
    _OW, _OH = 580, 640

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._cfg = router.get_config()
        self._active_tab = 0
        self._keys_masked = True
        self._active_tier = "chat"

        # Theme colors
        self._C_BG = "rgba(0, 7, 12, 248)"
        self._C_BORDER = "#1a5c7a"
        self._C_PRI = "#00d4ff"
        self._C_PRI_DIM = "#007a99"
        self._C_TEXT = "#8ffcff"
        self._C_TEXT_DIM = "#3a8a9a"
        self._C_GREEN = "#00ff88"
        self._C_RED = "#ff3355"

        self.setStyleSheet(f"""
            ModelRouterOverlay {{
                background: {self._C_BG};
                border: 1px solid {self._C_BORDER};
                border-radius: 6px;
            }}
            QLineEdit, QTextEdit, QComboBox, QListWidget {{
                background: #000f17;
                color: {self._C_TEXT};
                border: 1px solid {self._C_BORDER};
                border-radius: 4px;
                padding: 4px 8px;
                font-family: 'Courier New';
                font-size: 11px;
            }}
            QLineEdit:focus, QTextEdit:focus, QComboBox:focus, QListWidget:focus {{
                border: 1px solid {self._C_PRI};
            }}
            QComboBox QAbstractItemView {{
                background: #000f17;
                color: {self._C_TEXT};
                selection-background-color: {self._C_PRI_DIM};
            }}
        """)

        self._test_done.connect(self._on_test_done)
        self._ladder_test_done.connect(self._on_ladder_test_done)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(10)

        # ── Header ──────────────────────────────────────────────────────────
        hdr = QHBoxLayout()
        title = QLabel("⚡ SMART PROVIDER ROUTER & FALLBACK")
        title.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {self._C_PRI};")
        hdr.addWidget(title)
        hdr.addStretch(1)

        close_btn = QPushButton("✕")
        close_btn.setFixedSize(24, 24)
        close_btn.setFont(QFont("Courier New", 10, QFont.Weight.Bold))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {self._C_TEXT_DIM}; border: none; }}
            QPushButton:hover {{ color: {self._C_RED}; }}
        """)
        close_btn.clicked.connect(self.close)
        hdr.addWidget(close_btn)
        root.addLayout(hdr)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {self._C_BORDER};")
        root.addWidget(sep)

        # ── Tabs Navigation ──────────────────────────────────────────────────
        tab_row = QHBoxLayout()
        tab_row.setSpacing(6)
        self._tab_btn1 = self._make_tab_btn("🌐 1. PROVIDERS", 0)
        self._tab_btn2 = self._make_tab_btn("🔑 2. API KEYS", 1)
        self._tab_btn3 = self._make_tab_btn("🪜 3. LADDERS", 2)
        tab_row.addWidget(self._tab_btn1)
        tab_row.addWidget(self._tab_btn2)
        tab_row.addWidget(self._tab_btn3)
        root.addLayout(tab_row)

        # ── Content Panels ───────────────────────────────────────────────────
        self._panel_container = QVBoxLayout()
        self._panel1 = self._build_tab1_providers()
        self._panel2 = self._build_tab2_keys()
        self._panel3 = self._build_tab3_ladders()

        self._panel_container.addWidget(self._panel1)
        self._panel_container.addWidget(self._panel2)
        self._panel_container.addWidget(self._panel3)
        root.addLayout(self._panel_container, 1)

        # ── Footer & Save ───────────────────────────────────────────────────
        footer = QHBoxLayout()
        self._status_lbl = QLabel("Ready")
        self._status_lbl.setFont(QFont("Courier New", 9))
        self._status_lbl.setStyleSheet(f"color: {self._C_TEXT_DIM};")
        footer.addWidget(self._status_lbl, 1)

        save_btn = QPushButton("💾 SAVE ALL SETTINGS")
        save_btn.setFixedHeight(28)
        save_btn.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.setStyleSheet(f"""
            QPushButton {{
                background: {self._C_PRI_DIM};
                color: #ffffff;
                border: 1px solid {self._C_PRI};
                border-radius: 4px;
                padding: 0 16px;
            }}
            QPushButton:hover {{ background: {self._C_PRI}; color: #00060a; }}
        """)
        save_btn.clicked.connect(self._save_all)
        footer.addWidget(save_btn)
        root.addLayout(footer)

        self._set_tab(0)

    # ── Tab Navigation ───────────────────────────────────────────────────────

    def _make_tab_btn(self, text: str, index: int) -> QPushButton:
        btn = QPushButton(text)
        btn.setFixedHeight(28)
        btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(lambda: self._set_tab(index))
        return btn

    def _set_tab(self, index: int) -> None:
        self._active_tab = index
        tabs = [self._tab_btn1, self._tab_btn2, self._tab_btn3]
        panels = [self._panel1, self._panel2, self._panel3]
        for i, (btn, panel) in enumerate(zip(tabs, panels)):
            if i == index:
                btn.setStyleSheet(f"""
                    background: #002233; color: {self._C_PRI};
                    border: 1px solid {self._C_PRI}; border-radius: 4px;
                """)
                panel.show()
            else:
                btn.setStyleSheet(f"""
                    background: transparent; color: {self._C_TEXT_DIM};
                    border: 1px solid {self._C_BORDER}; border-radius: 4px;
                """)
                panel.hide()

    # ── Section 1: Provider Register ─────────────────────────────────────────

    def _build_tab1_providers(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.setSpacing(8)

        # Selector row
        sel_row = QHBoxLayout()
        sel_row.addWidget(self._lbl("Select Provider:", bold=True))
        self._p1_selector = QComboBox()
        self._p1_selector.currentIndexChanged.connect(self._on_p1_provider_selected)
        sel_row.addWidget(self._p1_selector, 1)

        new_btn = QPushButton("+ New")
        new_btn.setFixedSize(60, 26)
        new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        new_btn.setStyleSheet(self._btn_style(dim=True))
        new_btn.clicked.connect(self._on_p1_new_clicked)
        sel_row.addWidget(new_btn)
        lay.addLayout(sel_row)

        # Preset template row
        preset_row = QHBoxLayout()
        preset_row.addWidget(self._lbl("Load Preset:", color=self._C_TEXT_DIM))
        self._p1_preset = QComboBox()
        self._p1_preset.addItems(["(Choose Template)", "xAI Grok", "OpenRouter", "DeepSeek", "Hugging Face", "Ollama (Cloud)", "Ollama (Local)", "LM Studio (Local)", "Tavily Search"])
        self._p1_preset.currentIndexChanged.connect(self._on_p1_preset_selected)
        preset_row.addWidget(self._p1_preset, 1)
        lay.addLayout(preset_row)

        # Fields
        f_grid = QVBoxLayout()
        f_grid.setSpacing(5)

        self._p1_id = QLineEdit()
        self._p1_id.setPlaceholderText("provider_id (e.g. grok, openrouter)")
        f_grid.addWidget(self._lbl("Provider ID (Internal identifier):"))
        f_grid.addWidget(self._p1_id)

        self._p1_name = QLineEdit()
        self._p1_name.setPlaceholderText("Display Name (e.g. xAI Grok)")
        f_grid.addWidget(self._lbl("Display Name:"))
        f_grid.addWidget(self._p1_name)

        self._p1_type = QComboBox()
        self._p1_type.addItems(["openai", "gemini", "ollama", "search"])
        f_grid.addWidget(self._lbl("Provider Protocol Type:"))
        f_grid.addWidget(self._p1_type)

        self._p1_url = QLineEdit()
        self._p1_url.setPlaceholderText("https://api.x.ai/v1")
        f_grid.addWidget(self._lbl("Endpoint URL (Base URL):"))
        f_grid.addWidget(self._p1_url)

        self._p1_models = QLineEdit()
        self._p1_models.setPlaceholderText("model1, model2, model3")
        f_grid.addWidget(self._lbl("Supported Models (comma-separated):"))
        f_grid.addWidget(self._p1_models)

        lay.addLayout(f_grid)

        # Action Buttons
        btn_row = QHBoxLayout()
        test_btn = QPushButton("⚡ Test Connection")
        test_btn.setFixedHeight(26)
        test_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        test_btn.setStyleSheet(self._btn_style(dim=False))
        test_btn.clicked.connect(self._on_p1_test_clicked)
        btn_row.addWidget(test_btn)

        del_btn = QPushButton("🗑️ Delete Provider")
        del_btn.setFixedHeight(26)
        del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        del_btn.setStyleSheet(self._btn_style(color=self._C_RED))
        del_btn.clicked.connect(self._on_p1_delete_clicked)
        btn_row.addWidget(del_btn)
        lay.addLayout(btn_row)

        self._refresh_p1_providers()
        return w

    def _refresh_p1_providers(self) -> None:
        self._p1_selector.blockSignals(True)
        self._p1_selector.clear()
        providers = self._cfg.get("providers", {})
        for pid, pdata in providers.items():
            self._p1_selector.addItem(f"{pdata.get('name', pid)} [{pid}]", pid)
        self._p1_selector.blockSignals(False)
        self._on_p1_provider_selected()

    def _on_p1_provider_selected(self) -> None:
        pid = self._p1_selector.currentData()
        if not pid:
            return
        pdata = self._cfg.get("providers", {}).get(pid, {})
        self._p1_id.setText(pid)
        self._p1_name.setText(pdata.get("name", pid))
        idx = self._p1_type.findText(pdata.get("type", "openai"))
        if idx >= 0:
            self._p1_type.setCurrentIndex(idx)
        self._p1_url.setText(pdata.get("base_url", ""))
        self._p1_models.setText(", ".join(pdata.get("models", [])))

    def _on_p1_new_clicked(self) -> None:
        self._p1_id.setText("custom_provider")
        self._p1_name.setText("Custom Provider")
        self._p1_type.setCurrentIndex(0)
        self._p1_url.setText("https://api.example.com/v1")
        self._p1_models.setText("model-1, model-2")
        self._p1_id.setFocus()

    def _on_p1_preset_selected(self, idx: int) -> None:
        text = self._p1_preset.currentText()
        presets = {
            "xAI Grok": ("grok", "xAI Grok", "openai", "https://api.x.ai/v1", "grok-2-latest, grok-2-vision-1212, grok-beta"),
            "OpenRouter": ("openrouter", "OpenRouter", "openai", "https://openrouter.ai/api/v1", "meta-llama/llama-3.3-70b-instruct, anthropic/claude-3.5-sonnet, deepseek/deepseek-chat"),
            "DeepSeek": ("deepseek", "DeepSeek AI", "openai", "https://api.deepseek.com/v1", "deepseek-chat, deepseek-coder"),
            "Hugging Face": ("huggingface", "Hugging Face", "openai", "https://api-inference.huggingface.co/v1", "meta-llama/Llama-3.3-70B-Instruct, meta-llama/Llama-3.1-8B-Instruct, Qwen/Qwen2.5-72B-Instruct, Qwen/Qwen2.5-Coder-32B-Instruct, deepseek-ai/DeepSeek-R1-Distill-Qwen-32B, mistralai/Mistral-7B-Instruct-v0.3"),
            "Ollama (Cloud)": ("ollama_cloud", "Ollama Cloud", "ollama", "https://ollama.com", "gpt-oss:120b, gpt-oss:20b, deepseek-v4.1-flash, mistral-large-3:675b, kimi-k3, glm-5.3-flash"),
            "Ollama (Local)": ("ollama", "Ollama (Local)", "ollama", "http://localhost:11434", "llama3.2:latest, qwen2.5-coder:latest, mistral:latest"),
            "LM Studio (Local)": ("lmstudio", "LM Studio (Local)", "openai", "http://localhost:1234/v1", "local-model"),
            "Tavily Search": ("tavily", "Tavily Search", "search", "https://api.tavily.com", "tavily-search")
        }
        if text in presets:
            pid, name, ptype, url, models = presets[text]
            self._p1_id.setText(pid)
            self._p1_name.setText(name)
            t_idx = self._p1_type.findText(ptype)
            if t_idx >= 0:
                self._p1_type.setCurrentIndex(t_idx)
            self._p1_url.setText(url)
            self._p1_models.setText(models)

    def _on_p1_test_clicked(self) -> None:
        pid = self._p1_id.text().strip()
        if not pid:
            self._status_lbl.setText("Enter a valid Provider ID first.")
            return
        self._sync_p1_to_cfg()
        self._status_lbl.setText(f"Testing {pid}...")

        def _test():
            ok, msg = router.test_provider(pid)
            self._test_done.emit(ok, msg)

        threading.Thread(target=_test, daemon=True).start()

    def _on_test_done(self, ok: bool, msg: str) -> None:
        color = self._C_GREEN if ok else self._C_RED
        self._status_lbl.setStyleSheet(f"color: {color};")
        self._status_lbl.setText(f"Test Result: {msg}")

    def _on_p1_delete_clicked(self) -> None:
        pid = self._p1_selector.currentData()
        if pid in self._cfg.get("providers", {}):
            del self._cfg["providers"][pid]
            self._refresh_p1_providers()
            self._refresh_p2_providers()
            self._status_lbl.setText(f"Deleted provider '{pid}'.")

    def _sync_p1_to_cfg(self) -> None:
        pid = self._p1_id.text().strip()
        if not pid:
            return
        providers = self._cfg.setdefault("providers", {})
        existing = providers.get(pid, {})
        models = [m.strip() for m in self._p1_models.text().split(",") if m.strip()]
        providers[pid] = {
            "name": self._p1_name.text().strip() or pid,
            "type": self._p1_type.currentText(),
            "base_url": self._p1_url.text().strip(),
            "api_keys": existing.get("api_keys", []),
            "models": models,
            "enabled": True
        }

    # ── Section 2: API Key Setup ─────────────────────────────────────────────

    def _build_tab2_keys(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.setSpacing(8)

        # Provider selector
        p_row = QHBoxLayout()
        p_row.addWidget(self._lbl("Select Provider:", bold=True))
        self._p2_selector = QComboBox()
        self._p2_selector.currentIndexChanged.connect(self._on_p2_provider_selected)
        p_row.addWidget(self._p2_selector, 1)

        self._p2_pool_badge = QLabel("0 keys")
        self._p2_pool_badge.setStyleSheet(f"color: {self._C_PRI}; font-size: 10px;")
        p_row.addWidget(self._p2_pool_badge)
        lay.addLayout(p_row)

        desc = QLabel("Enter one or multiple API keys (one per line). Mark rotates keys automatically on 429 quota exhaustion.")
        desc.setFont(QFont("Courier New", 8))
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color: {self._C_TEXT_DIM};")
        lay.addWidget(desc)

        # Multi-key editor
        self._p2_keys_edit = QTextEdit()
        self._p2_keys_edit.setPlaceholderText("key_1\nkey_2\nkey_3...")
        self._p2_keys_edit.setFixedHeight(140)
        self._p2_keys_edit.textChanged.connect(self._on_p2_keys_changed)
        lay.addWidget(self._p2_keys_edit)

        # Mask toggle & rotation mode
        ctrl_row = QHBoxLayout()
        self._mask_btn = QPushButton("👁️ Show Keys")
        self._mask_btn.setFixedHeight(24)
        self._mask_btn.setStyleSheet(self._btn_style(dim=True))
        self._mask_btn.clicked.connect(self._toggle_key_mask)
        ctrl_row.addWidget(self._mask_btn)

        ctrl_row.addStretch(1)
        ctrl_row.addWidget(self._lbl("Rotation Strategy:"))
        self._p2_rotation = QComboBox()
        self._p2_rotation.addItems(["Failover on Quota", "Round-Robin Load Balance"])
        ctrl_row.addWidget(self._p2_rotation)
        lay.addLayout(ctrl_row)

        self._refresh_p2_providers()
        return w

    def _refresh_p2_providers(self) -> None:
        self._p2_selector.blockSignals(True)
        self._p2_selector.clear()
        providers = self._cfg.get("providers", {})
        for pid, pdata in providers.items():
            self._p2_selector.addItem(f"{pdata.get('name', pid)} [{pid}]", pid)
        self._p2_selector.blockSignals(False)
        self._on_p2_provider_selected()

    def _on_p2_provider_selected(self) -> None:
        pid = self._p2_selector.currentData()
        if not pid:
            return
        keys = self._cfg.get("providers", {}).get(pid, {}).get("api_keys", [])
        self._p2_keys_edit.blockSignals(True)
        self._p2_keys_edit.setPlainText("\n".join(keys))
        self._p2_keys_edit.blockSignals(False)
        self._p2_pool_badge.setText(f"{len(keys)} key(s) in pool")

    def _on_p2_keys_changed(self) -> None:
        pid = self._p2_selector.currentData()
        if not pid:
            return
        raw = self._p2_keys_edit.toPlainText().strip()
        lines = [k.strip() for k in raw.splitlines() if k.strip()]
        self._cfg.setdefault("providers", {}).setdefault(pid, {})["api_keys"] = lines
        self._p2_pool_badge.setText(f"{len(lines)} key(s) in pool")

    def _toggle_key_mask(self) -> None:
        self._keys_masked = not self._keys_masked
        self._mask_btn.setText("👁️ Show Keys" if self._keys_masked else "🔒 Mask Keys")
        # For simplicity in QTextEdit, visual hint in status
        self._status_lbl.setText("Keys unmasked" if not self._keys_masked else "Keys masked")

    # ── Section 3: Fallback Order ────────────────────────────────────────────

    def _build_tab3_ladders(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.setSpacing(8)

        # Capability Tier Selector
        tier_row = QHBoxLayout()
        tier_row.addWidget(self._lbl("Capability Tier:", bold=True))
        self._p3_tier = QComboBox()
        self._p3_tier.addItems(["chat (Chat & Reasoning)", "vision (Vision & Screenshots)", "fast (Fast Utility & Tools)", "search (Web Search)"])
        self._p3_tier.currentIndexChanged.connect(self._on_p3_tier_changed)
        tier_row.addWidget(self._p3_tier, 1)
        lay.addLayout(tier_row)

        # Ladder List
        self._ladder_list = QListWidget()
        self._ladder_list.setFixedHeight(180)
        lay.addWidget(self._ladder_list)

        # Reorder and edit controls
        btn_row = QHBoxLayout()
        up_btn = QPushButton("▲ Move Up")
        up_btn.setStyleSheet(self._btn_style(dim=True))
        up_btn.clicked.connect(self._on_ladder_move_up)
        btn_row.addWidget(up_btn)

        down_btn = QPushButton("▼ Move Down")
        down_btn.setStyleSheet(self._btn_style(dim=True))
        down_btn.clicked.connect(self._on_ladder_move_down)
        btn_row.addWidget(down_btn)

        del_rung_btn = QPushButton("✕ Remove Rung")
        del_rung_btn.setStyleSheet(self._btn_style(color=self._C_RED))
        del_rung_btn.clicked.connect(self._on_ladder_remove_rung)
        btn_row.addWidget(del_rung_btn)
        lay.addLayout(btn_row)

        # Add Step Row
        add_row = QHBoxLayout()
        add_row.addWidget(self._lbl("Add Step:"))
        self._add_provider_combo = QComboBox()
        self._add_provider_combo.currentIndexChanged.connect(self._on_add_provider_changed)
        add_row.addWidget(self._add_provider_combo, 1)

        self._add_model_combo = QComboBox()
        add_row.addWidget(self._add_model_combo, 1)

        add_btn = QPushButton("+ Add Rung")
        add_btn.setStyleSheet(self._btn_style(dim=False))
        add_btn.clicked.connect(self._on_ladder_add_rung)
        add_row.addWidget(add_btn)
        lay.addLayout(add_row)

        # Test ladder simulation
        test_ladder_btn = QPushButton("⚡ Simulate & Test Fallback Ladder")
        test_ladder_btn.setFixedHeight(26)
        test_ladder_btn.setStyleSheet(self._btn_style(dim=False))
        test_ladder_btn.clicked.connect(self._on_test_ladder_clicked)
        lay.addWidget(test_ladder_btn)

        self._refresh_p3_add_combos()
        self._refresh_ladder_list()
        return w

    def _on_p3_tier_changed(self) -> None:
        raw = self._p3_tier.currentText().split()[0]
        self._active_tier = raw
        self._refresh_ladder_list()

    def _refresh_ladder_list(self) -> None:
        self._ladder_list.clear()
        routing = self._cfg.get("routing", {}).get(self._active_tier, [])
        for i, rung in enumerate(routing, 1):
            p = rung.get("provider", "")
            m = rung.get("model", "")
            item = QListWidgetItem(f"{i}. [{p}]  {m}")
            item.setData(Qt.ItemDataRole.UserRole, rung)
            self._ladder_list.addItem(item)

    def _refresh_p3_add_combos(self) -> None:
        self._add_provider_combo.blockSignals(True)
        self._add_provider_combo.clear()
        providers = self._cfg.get("providers", {})
        for pid in providers.keys():
            self._add_provider_combo.addItem(pid)
        self._add_provider_combo.blockSignals(False)
        self._on_add_provider_changed()

    def _on_add_provider_changed(self) -> None:
        pid = self._add_provider_combo.currentText()
        models = self._cfg.get("providers", {}).get(pid, {}).get("models", [])
        self._add_model_combo.clear()
        self._add_model_combo.addItems(models if models else ["default"])

    def _on_ladder_move_up(self) -> None:
        row = self._ladder_list.currentRow()
        if row <= 0:
            return
        l = self._cfg.setdefault("routing", {}).setdefault(self._active_tier, [])
        l[row], l[row - 1] = l[row - 1], l[row]
        self._refresh_ladder_list()
        self._ladder_list.setCurrentRow(row - 1)

    def _on_ladder_move_down(self) -> None:
        row = self._ladder_list.currentRow()
        l = self._cfg.setdefault("routing", {}).setdefault(self._active_tier, [])
        if row < 0 or row >= len(l) - 1:
            return
        l[row], l[row + 1] = l[row + 1], l[row]
        self._refresh_ladder_list()
        self._ladder_list.setCurrentRow(row + 1)

    def _on_ladder_remove_rung(self) -> None:
        row = self._ladder_list.currentRow()
        l = self._cfg.setdefault("routing", {}).setdefault(self._active_tier, [])
        if 0 <= row < len(l):
            del l[row]
            self._refresh_ladder_list()

    def _on_ladder_add_rung(self) -> None:
        p = self._add_provider_combo.currentText()
        m = self._add_model_combo.currentText()
        if not p or not m:
            return
        l = self._cfg.setdefault("routing", {}).setdefault(self._active_tier, [])
        l.append({"provider": p, "model": m})
        self._refresh_ladder_list()

    def _on_test_ladder_clicked(self) -> None:
        tier = self._active_tier
        self._status_lbl.setText(f"Testing {tier} fallback ladder...")

        def _test():
            reply, rung_info = router.dispatch_chat("Test prompt. Respond with OK.", tier=tier, timeout=15)
            if reply:
                msg = f"Ladder Success! Handled by: {rung_info}"
            else:
                msg = f"All rungs on {tier} ladder failed or timed out."
            self._ladder_test_done.emit(msg)

        threading.Thread(target=_test, daemon=True).start()

    def _on_ladder_test_done(self, msg: str) -> None:
        self._status_lbl.setText(msg)

    # ── Global Save ──────────────────────────────────────────────────────────

    def _save_all(self) -> None:
        self._sync_p1_to_cfg()
        rot_val = "round_robin" if "Round-Robin" in self._p2_rotation.currentText() else "failover"
        self._cfg.setdefault("options", {})["key_rotation"] = rot_val
        ok = router.update_config(self._cfg)
        if ok:
            self._status_lbl.setStyleSheet(f"color: {self._C_GREEN};")
            self._status_lbl.setText("✓ Settings saved successfully to config/providers.json!")
        else:
            self._status_lbl.setStyleSheet(f"color: {self._C_RED};")
            self._status_lbl.setText("Failed to save settings.")

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _lbl(self, text: str, bold: bool = False, color: Optional[str] = None) -> QLabel:
        l = QLabel(text)
        l.setFont(QFont("Courier New", 9, QFont.Weight.Bold if bold else QFont.Weight.Normal))
        l.setStyleSheet(f"color: {color or self._C_TEXT}; background: transparent;")
        return l

    def _btn_style(self, dim: bool = False, color: Optional[str] = None) -> str:
        b_color = color or (self._C_PRI_DIM if dim else self._C_PRI)
        t_color = color or (self._C_TEXT if dim else "#00060a")
        bg_color = "transparent" if dim else self._C_PRI
        return f"""
            QPushButton {{
                background: {bg_color};
                color: {t_color};
                border: 1px solid {b_color};
                border-radius: 3px;
                padding: 3px 8px;
                font-family: 'Courier New';
                font-size: 10px;
            }}
            QPushButton:hover {{
                border-color: {self._C_PRI};
                color: {self._C_PRI};
                background: #001f2e;
            }}
        """
