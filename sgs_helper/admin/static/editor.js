/* 产品 / 武将专用录入页的交互。
 *
 * 只做这几件事，全部原生 JS：
 *   1. 搜索式组合框（产品 / 势力 / 武将牌系列 / 技能），支持 ↑↓ / Enter / Esc；
 *   2. 标签输入（产品的武将牌系列、游戏牌拓展包）；
 *   3. 武将技能行：增删 / 排序 / 版本选择 / 内联新建技能 / 内联新增版本；
 *   4. 同名武将提醒、相同技能版本提醒；
 *   5. Ctrl/⌘+S 保存、sessionStorage 草稿恢复；
 *   6. 以已有武将为模板时，勾选决定是否复制 势力/勾玉/性别/称号。
 */
(function () {
  "use strict";

  var debounce = function (fn, ms) {
    var timer = null;
    return function () {
      var args = arguments;
      clearTimeout(timer);
      timer = setTimeout(function () {
        fn.apply(null, args);
      }, ms);
    };
  };
  var escapeHtml = function (text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  };

  /* ------------------------------------------------------------------ 组合框 */

  function SearchCombo(root, options) {
    this.root = root;
    this.input = root.querySelector(".combo-input");
    this.hidden = root.querySelector(".combo-value");
    this.list = root.querySelector(".combo-list");
    this.options = options || {};
    this.items = [];
    this.active = -1;
    this.picked = null;
    if (!this.input || !this.hidden || !this.list) return;
    var self = this;

    this.input.addEventListener("input", debounce(function () {
      // 允许自由输入时（势力 / 系列），直接把文本当值
      if (self.options.freeText) self.hidden.value = self.input.value.trim();
      if (self.options.onInput) self.options.onInput(self.input.value.trim(), self);
      self.refresh();
    }, 160));
    this.input.addEventListener("focus", function () {
      self.refresh();
    });
    this.input.addEventListener("keydown", function (event) {
      self.onKeydown(event);
    });
    this.input.addEventListener("blur", function () {
      setTimeout(function () {
        self.close();
        if (self.options.freeText) self.hidden.value = self.input.value.trim();
      }, 140);
    });
    this.list.addEventListener("mousedown", function (event) {
      event.preventDefault(); // 别让输入框先失焦
    });
    this.list.addEventListener("click", function (event) {
      var node = event.target.closest("[data-pos]");
      if (node) self.pick(Number(node.dataset.pos));
    });
  }

  SearchCombo.prototype.setValue = function (value, display) {
    this.hidden.value = value || "";
    this.input.value = display == null ? value || "" : display;
    this.close();
  };

  SearchCombo.prototype.refresh = function () {
    var self = this;
    var query = this.input.value.trim();
    if (this.options.fetch) {
      this.options
        .fetch(query, this)
        .then(function (items) {
          self.items = items || [];
          self.render();
        })
        .catch(function () {
          self.items = [];
          self.render();
        });
    } else {
      this.items = this.options.staticItems || [];
      this.render();
    }
  };

  SearchCombo.prototype.render = function () {
    var self = this;
    var query = this.input.value.trim();
    var html = this.items
      .map(function (item, position) {
        var active = position === self.active ? " active" : "";
        return (
          '<div class="combo-item' + active + '" data-pos="' + position + '">' +
          '<div class="combo-label">' + escapeHtml(item.label) + "</div>" +
          (item.sub ? '<div class="combo-sub">' + escapeHtml(item.sub) + "</div>" : "") +
          "</div>"
        );
      })
      .join("");
    if (this.options.createLabel && query) {
      var position = this.items.length;
      var active = this.active === position ? " active" : "";
      html +=
        '<div class="combo-item combo-create' + active + '" data-pos="' + position + '">' +
        '<div class="combo-label">' + escapeHtml(this.options.createLabel(query)) + "</div></div>";
      this.items = this.items.concat([{ create: true, label: query }]);
    }
    if (!html) {
      html = '<div class="combo-empty">没有匹配项</div>';
    }
    this.list.innerHTML = html;
    this.list.hidden = false;
  };

  SearchCombo.prototype.close = function () {
    this.list.hidden = true;
    this.active = -1;
  };

  SearchCombo.prototype.onKeydown = function (event) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      if (this.list.hidden) this.refresh();
      var total = this.items.length;
      if (!total) return;
      event.preventDefault();
      this.active =
        event.key === "ArrowDown"
          ? (this.active + 1 + total) % total
          : (this.active - 1 + total) % total;
      this.render();
      var activeNode = this.list.querySelector(".combo-item.active");
      if (activeNode) activeNode.scrollIntoView({ block: "nearest" });
      return;
    }
    if (event.key === "Enter") {
      if (!this.list.hidden) {
        event.preventDefault();
        this.pick(this.active >= 0 ? this.active : 0);
      }
      return;
    }
    if (event.key === "Escape") {
      this.close();
    }
  };

  SearchCombo.prototype.pick = function (position) {
    var item = this.items[position];
    if (!item) return;
    if (item.create) {
      if (this.options.onCreate) this.options.onCreate(this.input.value.trim(), this);
    } else if (this.options.onPick) {
      this.options.onPick(item, this);
    } else {
      this.setValue(item.value, item.label);
    }
    this.close();
  };

  /* ------------------------------------------------------------------ 标签输入 */

  function TagInput(root) {
    this.root = root;
    this.hidden = document.getElementById(root.dataset.target);
    this.values = (this.hidden && this.hidden.value ? this.hidden.value.split("\n") : [])
      .map(function (item) { return item.trim(); })
      .filter(Boolean);
    this.suggestUrl = root.dataset.suggestUrl || "";
    this.staticValues = [];
    try {
      this.staticValues = JSON.parse(root.dataset.values || "[]");
    } catch (error) {
      this.staticValues = [];
    }
    this.placeholder = root.dataset.placeholder || "输入后回车添加";
    var self = this;

    root.classList.add("tag-input-ready");
    root.innerHTML =
      '<div class="tag-chips"></div>' +
      '<input class="tag-entry" type="text" autocomplete="off">';
    this.chips = root.querySelector(".tag-chips");
    this.entry = root.querySelector(".tag-entry");
    this.entry.placeholder = this.placeholder;
    this.suggestionBox = document.createElement("div");
    this.suggestionBox.className = "combo-list tag-suggest";
    this.suggestionBox.hidden = true;
    root.appendChild(this.suggestionBox);
    this.active = -1;
    this.suggestions = [];

    this.entry.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === "," || event.key === "，") {
        event.preventDefault();
        if (self.active >= 0 && self.suggestions[self.active]) {
          self.add(self.suggestions[self.active].value);
        } else {
          self.add(self.entry.value);
        }
        return;
      }
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        if (!self.suggestions.length) return;
        event.preventDefault();
        var total = self.suggestions.length;
        self.active = event.key === "ArrowDown" ? (self.active + 1 + total) % total : (self.active - 1 + total) % total;
        self.renderSuggestions();
        return;
      }
      if (event.key === "Escape") {
        self.hideSuggestions();
        return;
      }
      if (event.key === "Backspace" && !self.entry.value && self.values.length) {
        self.values.pop();
        self.sync();
      }
    });
    this.entry.addEventListener("input", debounce(function () {
      self.loadSuggestions();
    }, 160));
    this.entry.addEventListener("focus", function () {
      self.loadSuggestions();
    });
    this.entry.addEventListener("blur", function () {
      setTimeout(function () {
        if (self.entry.value.trim()) self.add(self.entry.value);
        self.hideSuggestions();
      }, 140);
    });
    this.suggestionBox.addEventListener("mousedown", function (event) {
      event.preventDefault();
    });
    this.suggestionBox.addEventListener("click", function (event) {
      var node = event.target.closest("[data-value]");
      if (node) self.add(node.dataset.value);
    });

    this.renderChips();
  }

  TagInput.prototype.renderChips = function () {
    var self = this;
    this.chips.innerHTML = this.values
      .map(function (value, position) {
        return (
          '<span class="tag-chip">' + escapeHtml(value) +
          '<button type="button" class="tag-remove" data-pos="' + position + '" title="移除">×</button></span>'
        );
      })
      .join("");
    this.chips.querySelectorAll(".tag-remove").forEach(function (button) {
      button.addEventListener("click", function () {
        self.values.splice(Number(button.dataset.pos), 1);
        self.sync();
      });
    });
  };

  TagInput.prototype.add = function (value) {
    var text = String(value || "").trim();
    this.entry.value = "";
    this.hideSuggestions();
    if (!text) return;
    if (this.values.indexOf(text) === -1) {
      this.values.push(text);
      this.sync();
    }
  };

  TagInput.prototype.sync = function () {
    if (this.hidden) this.hidden.value = this.values.join("\n");
    this.renderChips();
    if (window.__sgsDraftTouch) window.__sgsDraftTouch();
  };

  TagInput.prototype.loadSuggestions = function () {
    var self = this;
    var query = this.entry.value.trim();
    if (this.suggestUrl) {
      fetch(this.suggestUrl + "&q=" + encodeURIComponent(query))
        .then(function (response) { return response.json(); })
        .then(function (items) {
          self.suggestions = items.filter(function (item) {
            return self.values.indexOf(item.value) === -1;
          });
          self.active = self.suggestions.length ? 0 : -1;
          self.renderSuggestions();
        })
        .catch(function () { self.hideSuggestions(); });
      return;
    }
    this.suggestions = this.staticValues
      .filter(function (value) {
        return self.values.indexOf(value) === -1 && (!query || value.indexOf(query) !== -1);
      })
      .map(function (value) { return { value: value, label: value }; });
    this.active = this.suggestions.length ? 0 : -1;
    this.renderSuggestions();
  };

  TagInput.prototype.renderSuggestions = function () {
    var self = this;
    if (!this.suggestions.length) {
      this.hideSuggestions();
      return;
    }
    this.suggestionBox.innerHTML = this.suggestions
      .map(function (item, position) {
        return (
          '<div class="combo-item' + (position === self.active ? " active" : "") +
          '" data-value="' + escapeHtml(item.value) + '">' + escapeHtml(item.label) + "</div>"
        );
      })
      .join("");
    this.suggestionBox.hidden = false;
  };

  TagInput.prototype.hideSuggestions = function () {
    this.suggestionBox.hidden = true;
    this.active = -1;
  };

  /* ------------------------------------------------------------------ 通用小工具 */

  function jsonFetch(url) {
    return fetch(url, { headers: { Accept: "application/json" } }).then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    });
  }

  /* ------------------------------------------------------------------ 技能行（武将版本与卡牌效果共用） */

  function setupSkillRows(form) {
    var rowsBox = document.getElementById("skill-rows");
    if (!rowsBox) return null;
    var countInput = document.getElementById("skill-count");
    var template = document.getElementById("skill-row-template");
    var modeSelect = form.querySelector('[name="mode"]');
    var productHidden = form.querySelector('[name="product_id"]');

    function reindex() {
      var rows = Array.prototype.slice.call(rowsBox.querySelectorAll(".skill-row"));
      rows.forEach(function (row, position) {
        row.dataset.index = position;
        row.querySelector(".skill-no").textContent = "技能 " + (position + 1);
        row.querySelectorAll("[name]").forEach(function (element) {
          element.name = element.name.replace(/^skill_\d+_/, "skill_" + position + "_");
        });
        row.querySelectorAll("[id]").forEach(function (element) {
          element.id = element.id.replace(/^f-skill-\d+-/, "f-skill-" + position + "-");
        });
        row.querySelectorAll('label[for]').forEach(function (label) {
          label.setAttribute("for", label.getAttribute("for").replace(/^f-skill-\d+-/, "f-skill-" + position + "-"));
        });
        if (row._combo) {
          row._combo.input.id = "f-skill-" + position + "-combo";
        }
      });
      if (countInput) countInput.value = rows.length;
      if (window.__sgsDraftTouch) window.__sgsDraftTouch();
    }

    function fillVersions(row, versions, preselect) {
      var select = row.querySelector(".version-select");
      var current = select.value;
      var html = '<option value="">（不指定：按模式回退到该技能的唯一版本）</option>';
      versions.forEach(function (version) {
        // 版本标签与产品名/模式重复时不再重复显示（避免「群狼环鼎 · 国战 · 群狼环鼎」）
        var head = [];
        if (version.display_label) head.push(version.display_label);
        head.push(version.mode_label);
        if (version.product_name) head.push(version.product_name);
        var label = head.join(" · ") + " · " + version.text_excerpt;
        var preview =
          version.mode_label + " · " + (version.product_name || "未绑定产品") + "\n" +
          version.text_excerpt;
        html +=
          '<option value="' + escapeHtml(version.id) + '" data-preview="' + escapeHtml(preview) + '">' +
          escapeHtml(label) + "</option>";
      });
      select.innerHTML = html;
      var target = current || preselect || "";
      if (target) select.value = target;
      if (!select.value && versions.length) {
        // 只有一个“明显匹配”的版本时自动选中（同产品或同模式）
        var obvious = versions.filter(function (version) { return version.rank <= 1; });
        if (obvious.length === 1) select.value = obvious[0].id;
      }
      updatePreview(row);
    }

    function updatePreview(row) {
      var select = row.querySelector(".version-select");
      var preview = row.querySelector(".version-preview");
      if (!select || !preview) return;
      var option = select.options[select.selectedIndex];
      var text = option && option.dataset ? option.dataset.preview : "";
      preview.textContent = text || "";
      if (text) {
        var bound = row.querySelector('[name$="_add_version_from"]');
        if (bound && !bound.value) bound.value = select.value;
      }
    }

    function loadVersions(row, skillId, preselect) {
      var select = row.querySelector(".version-select");
      if (!skillId) {
        select.innerHTML = '<option value="">（先选择技能）</option>';
        setHidden(row.querySelector(".version-field"), true);
        setHidden(row.querySelector(".add-version-field"), true);
        return;
      }
      setHidden(row.querySelector(".version-field"), false);
      setHidden(row.querySelector(".add-version-field"), false);
      var mode = modeSelect ? modeSelect.value : "";
      var productId = productHidden ? productHidden.value : "";
      jsonFetch(
        "/api/skills/" + encodeURIComponent(skillId) + "/versions?mode=" +
          encodeURIComponent(mode) + "&product_id=" + encodeURIComponent(productId)
      )
        .then(function (data) { fillVersions(row, data.versions || [], preselect); })
        .catch(function () {});
    }

    function setHidden(node, hidden) {
      // 卡牌页的技能行只有「技能 + 版本」两个字段，装饰性的区块不存在，必须容忍缺元素
      if (node) node.hidden = hidden;
    }

    function switchRowMode(row, isNew) {
      setHidden(row.querySelector(".version-field"), isNew);
      setHidden(row.querySelector(".add-version-field"), isNew);
      setHidden(row.querySelector(".new-skill-field"), !isNew);
    }

    function setupRow(row) {
      var skillValue = row.querySelector('[name$="_skill_id"]');
      var newNameInput = row.querySelector(".combo-new-name");
      var comboRoot = row.querySelector(".combo");
      // 只有武将录入页的整行技能支持「就地新建技能」；卡牌页只有「选已有技能 + 选版本」
      var canCreate = !!row.querySelector(".new-skill-field");

      var combo = new SearchCombo(comboRoot, {
        createLabel: canCreate
          ? function (query) { return "+ 新建技能「" + query + "」"; }
          : null,
        fetch: function (query) {
          return jsonFetch("/api/skills?q=" + encodeURIComponent(query)).then(function (items) {
            return items.map(function (item) {
              return {
                value: item.id,
                label: item.name,
                sub: "已有 " + item.version_count + " 个版本 · " + item.id,
                raw: item,
              };
            });
          });
        },
        onPick: function (item) {
          skillValue.value = item.value;
          if (newNameInput) newNameInput.value = "";
          combo.setValue(item.value, item.label);
          switchRowMode(row, false);
          loadVersions(row, item.value, "");
          if (window.__sgsDraftTouch) window.__sgsDraftTouch();
        },
        onCreate: function (name) {
          skillValue.value = "";
          if (newNameInput) newNameInput.value = name;
          combo.setValue("", name);
          switchRowMode(row, true);
          var newText = row.querySelector('[name$="_new_text"]');
          if (newText) newText.focus();
          if (window.__sgsDraftTouch) window.__sgsDraftTouch();
        },
        onInput: function (text) {
          if (!skillValue.value && newNameInput) {
            newNameInput.value = text;
            switchRowMode(row, true);
          }
        },
      });
      row._combo = combo;

      var select = row.querySelector(".version-select");
      if (select) {
        select.addEventListener("change", function () {
          updatePreview(row);
          var bound = row.querySelector('[name$="_add_version_from"]');
          if (bound) bound.value = select.value;
          if (window.__sgsDraftTouch) window.__sgsDraftTouch();
        });
      }

      var textToggle = row.querySelector(".toggle-skill-text");
      if (textToggle) {
        textToggle.addEventListener("click", function () {
          var box = row.querySelector(".skill-text-box");
          if (box) box.hidden = !box.hidden;
        });
      }
      var toggle = row.querySelector(".toggle-add-version");
      if (toggle) {
        toggle.addEventListener("click", function () {
          var box = row.querySelector(".add-version-box");
          box.hidden = !box.hidden;
          if (!box.hidden) {
            var text = row.querySelector('[name$="_add_version_text"]');
            if (text) text.focus();
          }
        });
      }

      var fromCurrent = row.querySelector(".from-current");
      if (fromCurrent) {
        fromCurrent.addEventListener("click", function () {
          var text = row.querySelector('[name$="_add_version_text"]');
          var select = row.querySelector(".version-select");
          var option = select.options[select.selectedIndex];
          if (option && option.value) {
            // 复制当前版本的文本：需要后端拿文本，这里用版本列表里的预览代替不了，
            // 所以向后端要一次完整文本
            jsonFetch("/api/skills/" + encodeURIComponent(skillValue.value) + "/versions")
              .then(function (data) {
                var found = (data.versions || []).filter(function (v) { return v.id === option.value; })[0];
                if (found && found.text) text.value = found.text;
                var bound = row.querySelector('[name$="_add_version_from"]');
                if (bound) bound.value = option.value;
                text.focus();
                if (window.__sgsDraftTouch) window.__sgsDraftTouch();
              })
              .catch(function () {});
          } else {
            text.focus();
          }
        });
      }

      var removeButton = row.querySelector(".row-remove");
      if (removeButton) {
        removeButton.addEventListener("click", function () {
          if (row.querySelector('[name$="_skill_id"]').value || row.querySelector(".combo-input").value) {
            if (!window.confirm("删除这一行技能？只会删掉这条记录与技能的引用关系，不会删除全局技能。")) return;
          }
          row.remove();
          reindex();
        });
      }
      var up = row.querySelector(".row-up");
      if (up) {
        up.addEventListener("click", function () {
          var previous = row.previousElementSibling;
          if (previous) {
            rowsBox.insertBefore(row, previous);
            reindex();
          }
        });
      }
      var down = row.querySelector(".row-down");
      if (down) {
        down.addEventListener("click", function () {
          var next = row.nextElementSibling;
          if (next) {
            rowsBox.insertBefore(next, row);
            reindex();
          }
        });
      }

      if (skillValue.value) {
        loadVersions(row, skillValue.value, row.querySelector(".version-select").value);
      } else {
        switchRowMode(row, true);
      }
    }

    function addRow() {
      var index = rowsBox.querySelectorAll(".skill-row").length;
      var holder = document.createElement("div");
      holder.innerHTML = template.innerHTML.replace(/__i__/g, String(index));
      var row = holder.firstElementChild;
      rowsBox.appendChild(row);
      setupRow(row);
      reindex();
      row.querySelector(".combo-input").focus();
      return row;
    }

    function rebuildRows(total) {
      rowsBox.innerHTML = "";
      for (var index = 0; index < total; index += 1) {
        var holder = document.createElement("div");
        holder.innerHTML = template.innerHTML.replace(/__i__/g, String(index));
        var row = holder.firstElementChild;
        rowsBox.appendChild(row);
        setupRow(row);
      }
      reindex();
    }

    rowsBox.querySelectorAll(".skill-row").forEach(setupRow);
    var addRowButton = document.getElementById("add-skill-row");
    if (addRowButton) {
      addRowButton.addEventListener("click", function () {
        addRow();
      });
    }
    reindex();

    // Ctrl/⌘ + Enter：快速加下一个技能；技能描述里按 Ctrl+Enter 也一样
    form.addEventListener("keydown", function (event) {
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
        event.preventDefault();
        addRow();
      }
    });

    return { rowsBox: rowsBox, reindex: reindex, rebuildRows: rebuildRows, addRow: addRow };
  }

  /* ------------------------------------------------------------------ 武将录入页 */

  function setupGeneralEditor(form) {
    var skillRows = setupSkillRows(form);
    var rowsBox = skillRows.rowsBox;
    var reindex = skillRows.reindex;
    var rebuildRows = skillRows.rebuildRows;
    var modeSelect = form.querySelector('[name="mode"]');
    var productHidden = form.querySelector('[name="product_id"]');

    var combos = {};

    /* ---- 产品组合框：自动带入模式 / 系列（只填空着的字段） */
    var productComboRoot = form.querySelector('[data-combo="product"]');
    if (productComboRoot) {
      combos.product = new SearchCombo(productComboRoot, {
        fetch: function (query) {
          return jsonFetch("/api/products?q=" + encodeURIComponent(query)).then(function (items) {
            return items.map(function (item) {
              return {
                value: item.id,
                label: item.name,
                sub: [item.release_year, item.mode_label, (item.general_sets || []).join("、")]
                  .filter(Boolean)
                  .join(" · "),
                raw: item,
              };
            });
          });
        },
        onPick: function (item) {
          combos.product.setValue(item.value, item.label);
          var raw = item.raw || {};
          if (raw.mode && modeSelect && !modeSelect.value) modeSelect.value = raw.mode;
          // 武将牌系列的候选完全来自所选产品的 Product.general_sets
          if (combos.setGeneralSetCandidates) {
            combos.setGeneralSetCandidates(raw.general_sets || []);
          }
          if (window.__sgsDraftTouch) window.__sgsDraftTouch();
        },
      });
    }

    /* ---- 势力：可搜索已有值，也可自由输入 */
    function setupSuggestCombo(kind, field) {
      var root = form.querySelector('[data-combo="' + kind + '"]');
      if (!root) return null;
      return new SearchCombo(root, {
        freeText: true,
        fetch: function (query) {
          return jsonFetch(
            "/api/suggest?field=" + field + "&q=" + encodeURIComponent(query)
          ).then(function (items) {
            return items.map(function (item) {
              return { value: item.value, label: item.value, sub: "已用 " + item.count + " 次" };
            });
          });
        },
        onPick: function (item, combo) {
          combo.setValue(item.value, item.value);
          if (window.__sgsDraftTouch) window.__sgsDraftTouch();
        },
      });
    }
    combos.faction = setupSuggestCombo("faction", "faction");

    /* ---- 武将牌系列：候选来自所选产品的 Product.general_sets */
    (function setupGeneralSetCombo() {
      var root = form.querySelector('[data-combo="general_set"]');
      if (!root) return;
      var productSets = null;      // 当前产品声明的系列（所选产品的权威候选）
      var allSets = null;          // 没选产品时的兜底：所有已知系列
      combos.general_set = new SearchCombo(root, {
        freeText: true,
        fetch: function (query) {
          if (productSets) {
            return Promise.resolve(
              productSets
                .filter(function (value) { return !query || value.indexOf(query) !== -1; })
                .map(function (value) {
                  return { value: value, label: value, sub: "该产品的武将牌系列" };
                })
            );
          }
          if (allSets) {
            return Promise.resolve(
              allSets
                .filter(function (value) { return !query || value.indexOf(query) !== -1; })
                .map(function (value) {
                  return { value: value, label: value, sub: "已有武将牌系列" };
                })
            );
          }
          return jsonFetch("/api/suggest?field=general_set&q=" + encodeURIComponent(query))
            .then(function (items) {
              allSets = items.map(function (item) { return item.value; });
              return items.map(function (item) {
                return { value: item.value, label: item.value, sub: "已用 " + item.count + " 次" };
              });
            });
        },
        onPick: function (item, combo) {
          combo.setValue(item.value, item.value);
          if (window.__sgsDraftTouch) window.__sgsDraftTouch();
        },
      });

      combos.setGeneralSetCandidates = function (sets) {
        productSets = (sets && sets.length) ? sets : null;
        allSets = null;
      };

      // 编辑已有记录：按当前产品的 general_sets 准备候选；输入框里已有当前值即为预选
      var currentProduct = productHidden ? productHidden.value : "";
      if (currentProduct) {
        jsonFetch("/api/products/" + encodeURIComponent(currentProduct))
          .then(function (raw) { combos.setGeneralSetCandidates(raw.general_sets || []); })
          .catch(function () {});
      }
    })();

    /* ---- 同名武将提醒（只提醒，不阻止） */
    var nameInput = form.querySelector('[name="name"]');
    var duplicatePanel = document.getElementById("duplicate-panel");
    var selfId = (form.querySelector('[name="id"]') || {}).value || "";
    if (nameInput && duplicatePanel) {
      nameInput.addEventListener(
        "input",
        debounce(function () {
          var value = nameInput.value.trim();
          if (value.length < 2) {
            duplicatePanel.hidden = true;
            return;
          }
          jsonFetch("/api/generals?name=" + encodeURIComponent(value))
            .then(function (items) {
              items = items.filter(function (item) { return item.id !== selfId; });
              if (!items.length) {
                duplicatePanel.hidden = true;
                return;
              }
              duplicatePanel.innerHTML =
                "<strong>发现已有同名武将</strong>（同名在不同产品/版本下是合法的，确认不是误录即可继续）：" +
                '<ul class="tight">' +
                items
                  .map(function (item) {
                    return (
                      "<li><a href=\"/view/general/" + escapeHtml(item.id) + "\" target=\"_blank\">" +
                      escapeHtml(item.name) + "</a> · " + escapeHtml(item.mode_label || "未填模式") +
                      " · " + escapeHtml(item.product_name || "未绑定产品") +
                      " · " + escapeHtml(item.general_set || "未填系列") +
                      " · " + escapeHtml(item.general_number || "无编号") + "</li>"
                    );
                  })
                  .join("") +
                "</ul>";
              duplicatePanel.hidden = false;
            })
            .catch(function () {});
        }, 250)
      );
    }

    /* ---- 相同技能版本提醒（新增版本时） */
    form.addEventListener(
      "blur",
      function (event) {
        var node = event.target;
        if (!node.name || node.name.indexOf("_add_version_text") === -1) return;
        var row = node.closest(".skill-row");
        if (!row) return;
        var skillId = row.querySelector('[name$="_skill_id"]').value;
        var text = node.value.trim();
        if (!skillId || !text) return;
        var mode = modeSelect ? modeSelect.value : "";
        var productId = productHidden ? productHidden.value : "";
        jsonFetch(
          "/api/version-exists?skill_id=" + encodeURIComponent(skillId) +
            "&mode=" + encodeURIComponent(mode) +
            "&product_id=" + encodeURIComponent(productId) +
            "&text=" + encodeURIComponent(text)
        )
          .then(function (data) {
            var hint = row.querySelector(".duplicate-version");
            if (!hint) {
              hint = document.createElement("p");
              hint.className = "field-error duplicate-version";
              row.querySelector(".add-version-box").appendChild(hint);
            }
            if (data.exists) {
              hint.textContent =
                "已有内容完全相同的技能版本 " + data.version.id +
                "，保存时会直接复用它（不会新建重复版本）。";
              hint.hidden = false;
            } else {
              hint.hidden = true;
            }
          })
          .catch(function () {});
      },
      true
    );

    /* ---- 模板勾选：取消勾选就清空对应字段 */
    var copyPanel = form.querySelector(".copy-panel");
    if (copyPanel) {
      var sourceValues = {};
      try {
        sourceValues = JSON.parse(copyPanel.dataset.sourceValues || "{}");
      } catch (error) {
        sourceValues = {};
      }
      copyPanel.querySelectorAll(".copy-toggle").forEach(function (checkbox) {
        checkbox.addEventListener("change", function () {
          var fieldName = checkbox.dataset.field;
          var target = form.querySelector('[name="' + fieldName + '"]');
          if (!target) return;
          if (checkbox.checked) {
            target.value = sourceValues[fieldName] || "";
          } else {
            target.value = "";
          }
          if (fieldName === "faction" && combos.faction) {
            combos.faction.setValue(target.value, target.value);
          }
          if (window.__sgsDraftTouch) window.__sgsDraftTouch();
        });
      });
    }

    /* ---- 草稿（sessionStorage）+ Ctrl/⌘+S */
    setupDraft(form, function (state) {
      if (state.skill_count != null) {
        var total = Math.max(0, Math.min(50, Number(state.skill_count) || 0));
        if (total !== rowsBox.querySelectorAll(".skill-row").length) rebuildRows(total);
      }
      reindex();
    });
  }

  /* ------------------------------------------------------------------ 卡牌录入页（联动分类 / 卡图 / 拓展包） */

  function setupCardEditor(form) {
    // 规则联动先独立启动，避免其他录入组件的问题使它失效。
    var useRows = setupCardRules(form);
    var skillRows = setupSkillRows(form);

    var categoryData = { tree: {}, triples: [] };
    var holder = document.getElementById("card-category-data");
    if (holder) {
      try {
        categoryData = JSON.parse(holder.textContent || "{}") || categoryData;
      } catch (error) {
        categoryData = { tree: {}, triples: [] };
      }
    }
    var tree = categoryData.tree || {};
    var triples = categoryData.triples || [];

    function uniquePush(list, value) {
      if (value && list.indexOf(value) === -1) list.push(value);
    }

    // 一级：分类树的一级键 ∪ 现有卡牌数据里出现过的一级值
    function level1Options() {
      var out = Object.keys(tree);
      triples.forEach(function (triple) { uniquePush(out, triple[0]); });
      return out;
    }

    // 二级：由一级决定（分类树 + 现有数据里同一级下出现过的二级值）
    function level2Options(category1) {
      var out = [];
      if (category1 && tree[category1]) {
        Object.keys(tree[category1]).forEach(function (value) { uniquePush(out, value); });
      }
      triples.forEach(function (triple) {
        if (!category1 || triple[0] === category1) uniquePush(out, triple[1]);
      });
      return out;
    }

    // 三级：由一 / 二级共同决定。
    // 基本牌在树里只有 杀 / 闪 / 桃 / 酒 四个牌族，所以候选里不会出现「火杀」（那是 name）。
    function level3Options(category1, category2) {
      var out = [];
      if (category1 && tree[category1]) {
        if (category2 && tree[category1][category2]) {
          tree[category1][category2].forEach(function (value) { uniquePush(out, value); });
        } else if (!category2) {
          Object.keys(tree[category1]).forEach(function (key) {
            (tree[category1][key] || []).forEach(function (value) { uniquePush(out, value); });
          });
        }
      }
      triples.forEach(function (triple) {
        if ((!category1 || triple[0] === category1) && (!category2 || triple[1] === category2)) {
          uniquePush(out, triple[2]);
        }
      });
      return out;
    }

    // 可输入可选的联动下拉（候选是静态算出来的，每次打开/输入时按当前上级值重算）
    function staticCombo(name, optionsFn) {
      var root = form.querySelector('[data-combo="' + name + '"]');
      if (!root) return null;
      return new SearchCombo(root, {
        freeText: true,
        fetch: function (query) {
          return Promise.resolve(
            optionsFn()
              .filter(function (value) { return !query || value.indexOf(query) !== -1; })
              .map(function (value) { return { value: value, label: value }; })
          );
        },
        onPick: function (item, combo) {
          combo.setValue(item.value, item.value);
          form.dispatchEvent(new Event("change"));
          if (window.__sgsDraftTouch) window.__sgsDraftTouch();
        },
      });
    }

    var combo1 = staticCombo("category_1", level1Options);
    var combo2 = staticCombo("category_2", function () {
      return level2Options(combo1 ? combo1.hidden.value : "");
    });
    staticCombo("category_3", function () {
      var category1 = combo1 ? combo1.hidden.value : "";
      var category2 = combo2 ? combo2.hidden.value : "";
      return level3Options(category1, category2);
    });

    /* ---- 所属拓展包：候选 = 所有产品的 card_sets ∪ 已有卡牌的 card_set */
    var setRoot = form.querySelector('[data-combo="card_set"]');
    if (setRoot) {
      new SearchCombo(setRoot, {
        freeText: true,
        fetch: function (query) {
          return jsonFetch("/api/suggest?field=card_set&q=" + encodeURIComponent(query))
            .then(function (items) {
              return items.map(function (item) {
                return { value: item.value, label: item.value, sub: "已用 " + item.count + " 次" };
              });
            });
        },
        onPick: function (item, combo) {
          combo.setValue(item.value, item.value);
          if (window.__sgsDraftTouch) window.__sgsDraftTouch();
        },
      });
    }

    /* ---- 卡图：候选 = data/card_images/ 下真实存在的文件名（可以留空 = 卡图待补） */
    var imageRoot = form.querySelector('[data-combo="image"]');
    if (imageRoot) {
      new SearchCombo(imageRoot, {
        freeText: true,
        fetch: function (query) {
          return jsonFetch("/api/card-images?q=" + encodeURIComponent(query))
            .then(function (items) {
              return items.map(function (item) {
                return { value: item.value, label: item.value, sub: item.sub || "data/card_images/" };
              });
            });
        },
        onPick: function (item, combo) {
          combo.setValue(item.value, item.value);
          if (window.__sgsDraftTouch) window.__sgsDraftTouch();
        },
      });
    }

    // 自由输入的组合框：提交前把输入框里的文本同步进 hidden，
    // 避免「在自由文本框里打了字但没在下拉里点选」导致字段丢失。
    var freeTextFields = ["category_1", "category_2", "category_3", "card_set", "image"];
    form.addEventListener("submit", function () {
      freeTextFields.forEach(function (name) {
        var root = form.querySelector('[data-combo="' + name + '"]');
        if (!root) return;
        var input = root.querySelector(".combo-input");
        var hidden = root.querySelector(".combo-value");
        if (input && hidden) hidden.value = input.value.trim();
      });
    });

    /* ---- 草稿（sessionStorage）+ Ctrl/⌘+S */
    setupDraft(form, function (state) {
      if (state.use_count != null) useRows.rebuild(Number(state.use_count) || 0);
      if (!skillRows) return;
      if (state.skill_count != null) {
        var total = Math.max(0, Math.min(50, Number(state.skill_count) || 0));
        if (total !== skillRows.rowsBox.querySelectorAll(".skill-row").length) {
          skillRows.rebuildRows(total);
        }
      }
      skillRows.reindex();
    });
    document.getElementById("card-interaction-status").hidden = true;
  }

  /* ------------------------------------------------------------------ 草稿 + 快捷键 */

  function setupCardRules(form) {
    var box = document.getElementById("card-use-rows");
    var count = document.getElementById("card-use-count");
    var template = document.getElementById("use-row-template");
    var section = document.getElementById("card-rules");
    var notice = document.getElementById("inactive-rules-notice");
    var consent = document.getElementById("confirm-inactive-rules");
    var effectPanel = document.getElementById("card-effect-panel");
    // 已有卡面原文必须保持展开，否则「有内容却看不到、改不了、删不掉」。
    var effectHasContent = effectPanel && effectPanel.dataset.hasContent === "1";
    var previousProfile = null;
    var previousInactive = "";
    var kinds = { weapon: "攻击范围", deck: "牌堆信息", use: "使用说明", note: "备注", card_set: "所属拓展包" };
    function rows() { return Array.from(box.querySelectorAll(".use-row")); }
    function reindex() {
      var all = rows();
      all.forEach(function (row, index) {
        row.dataset.index = String(index);
        row.querySelector(".use-no").textContent = all.length > 1 ? "使用方式 " + (index + 1) : "使用说明";
        row.querySelector(".use-remove").hidden = all.length <= 1;
        row.querySelector("[data-use-label]").hidden = all.length <= 1 && !row.querySelector('[data-use-field="label"]').value.trim();
        row.querySelectorAll("textarea").forEach(function (input) {
          var field = input.dataset.useField;
          var oldId = input.id;
          input.name = "use_" + index + "_" + field;
          input.id = "f-use-" + index + "-" + field;
          var label = row.querySelector('label[for="' + oldId + '"]');
          if (label) label.htmlFor = input.id;
        });
      });
      count.value = String(all.length);
      document.getElementById("add-use-row").disabled = all.length >= 50;
    }
    function add() {
      if (rows().length >= 50) return;
      box.appendChild(template.content.cloneNode(true));
      reindex();
    }
    function category(level) {
      var input = form.querySelector('[data-combo="category_' + level + '"] .combo-input');
      return input ? input.value.trim() : "";
    }
    function filled(node) {
      return Array.from(node.querySelectorAll("textarea")).some(function (input) { return input.value.trim(); });
    }
    function profile() {
      if (category(1) !== "游戏牌") return "free";
      if (category(2) === "基本牌" || category(2) === "锦囊牌") return "use";
      if (category(2) !== "装备牌") return "generic";
      if (category(3) === "武器") return "weapon";
      if (category(3) === "坐骑") return "mount";
      if (category(3) === "防具" || category(3) === "宝物") return "equipment";
      return "generic";
    }
    function active(kind, current) {
      return current !== "free" && (current === "generic" || kind === "note" || kind === "deck" || kind === current);
    }
    function update() {
      var current = profile();
      if (current === "use" && !rows().length) add();
      reindex();
      section.hidden = current === "free";
      var cardSetField = form.querySelector("[data-card-set-field]");
      cardSetField.hidden = current === "free";
      document.getElementById("card-note-panel").hidden = false;
      document.getElementById("generic-rule-summary").hidden = current !== "generic";
      if (current !== previousProfile) {
        document.getElementById("game-rule-fields").open = current !== "generic" || filled(section);
      }
      var inactive = [];
      section.querySelectorAll("[data-rule-kind]").forEach(function (node) {
        var kind = node.dataset.ruleKind;
        node.hidden = !active(kind, current);
        if (node.hidden && filled(node)) inactive.push(kinds[kind]);
      });
      var cardSet = form.querySelector('[name="card_set"]');
      if (current === "free" && cardSet && cardSet.value.trim()) inactive.push(kinds.card_set);
      var summary = inactive.join("、");
      if (current !== previousProfile || summary !== previousInactive) consent.checked = false;
      consent.required = Boolean(summary);
      notice.hidden = !summary;
      document.getElementById("inactive-rules-summary").textContent = summary ? summary + "不属于当前类型，已暂存。切回原类型可以恢复；保存当前类型将舍弃这些规则。" : "";
      if (current !== previousProfile) effectPanel.open = current !== "use" || effectHasContent;
      document.getElementById("card-effect-summary").textContent = current === "use"
        ? (effectHasContent ? "原始卡面文字（已有内容，可修改或清空）" : "原始卡面文字（可选）")
        : "效果";
      document.getElementById("card-effect-label").textContent = current === "use" ? "原始卡面文字（可空）" : "效果（可空）";
      document.getElementById("card-effect-help").textContent = current === "use" ?
        "完整卡面原文可以留在这里；上方分别填写使用时机、目标和效果。" :
        "填写卡面上的效果原文。";
      previousProfile = current;
      previousInactive = summary;
    }
    document.getElementById("add-use-row").addEventListener("click", function () {
      add();
      update();
      rows()[rows().length - 1].querySelector('[data-use-field="timing"]').focus();
      if (window.__sgsDraftTouch) window.__sgsDraftTouch();
    });
    box.addEventListener("click", function (event) {
      var button = event.target.closest(".use-remove");
      if (!button || rows().length <= 1) return;
      var row = button.closest(".use-row");
      if (filled(row) && !window.confirm("删除这组使用说明？其余使用方式会保留。")) return;
      row.remove();
      update();
      if (window.__sgsDraftTouch) window.__sgsDraftTouch();
    });
    form.addEventListener("input", update);
    form.addEventListener("change", update);
    form.addEventListener("sgs-draft-restored", update);
    // DOM 中的隐藏内容供切回类型和草稿恢复；只把当前类型的字段提交给服务端。
    form.addEventListener("formdata", function (event) {
      var current = profile();
      if (!active("weapon", current)) event.formData.set("attack_range", "");
      if (current === "free") {
        event.formData.set("deck_info", "");
        event.formData.set("card_set", "");
      }
      if (!active("use", current)) {
        event.formData.set("use_count", "0");
        Array.from(event.formData.keys()).forEach(function (key) {
          if (/^use_\d+_/.test(key)) event.formData.delete(key);
        });
      }
    });
    update();
    return { rebuild: function (total) {
      box.replaceChildren();
      for (var index = 0; index < Math.min(50, Math.max(0, total)); index++) add();
    }, update: update };
  }

  function setupDraft(form, beforeApply) {
    var key = "sgs-draft:" + form.getAttribute("action");
    var state = null;

    function collect() {
      var data = {};
      form.querySelectorAll("input[name], select[name], textarea[name]").forEach(function (element) {
        if (element.name === "_csrf") return; // 服务重启后使用当前页面的 token。
        if (element.type === "checkbox") {
          data[element.name] = element.checked ? element.value || "1" : "";
        } else if (element.type !== "submit" && element.type !== "button") {
          data[element.name] = element.value;
        }
      });
      var displays = {};
      form.querySelectorAll(".combo-input").forEach(function (element) {
        if (element.id) displays[element.id] = element.value;
      });
      return { data: data, displays: displays, savedAt: new Date().toISOString() };
    }

    var save = debounce(function () {
      try {
        sessionStorage.setItem(key, JSON.stringify(collect()));
      } catch (error) {
        /* 隐私模式等场景忽略 */
      }
    }, 700);
    window.__sgsDraftTouch = save;
    form.addEventListener("input", save);
    form.addEventListener("change", save);

    function apply(payload) {
      if (beforeApply) beforeApply(payload.data || {});
      Object.keys(payload.data || {}).forEach(function (name) {
        if (name === "_csrf") return; // 兼容旧草稿，避免恢复已失效的 token。
        var elements = form.querySelectorAll('[name="' + name.replace(/"/g, '\\"') + '"]');
        elements.forEach(function (element) {
          if (element.type === "checkbox") {
            element.checked = Boolean(payload.data[name]);
          } else {
            element.value = payload.data[name];
          }
        });
      });
      Object.keys(payload.displays || {}).forEach(function (id) {
        var element = document.getElementById(id);
        if (element) element.value = payload.displays[id];
      });
      form.dispatchEvent(new Event("sgs-draft-restored"));
      form.querySelectorAll(".tag-input").forEach(function (node) {
        if (node._tagInput) node._tagInput.sync();
      });
      form.querySelectorAll(".version-select").forEach(function (select) {
        var event = new Event("change");
        select.dispatchEvent(event);
      });
    }

    try {
      state = JSON.parse(sessionStorage.getItem(key) || "null");
    } catch (error) {
      state = null;
    }
    if (state && state.data) {
      var banner = document.createElement("div");
      banner.className = "banner warn";
      banner.innerHTML =
        "发现未保存的草稿（" + (state.savedAt || "").slice(0, 19).replace("T", " ") +
        "）。" +
        '<button type="button" class="mini-btn" data-draft="restore">恢复草稿</button> ' +
        '<button type="button" class="mini-btn" data-draft="discard">丢弃草稿</button>' +
        '<div class="muted">草稿只存在这个浏览器里，不会覆盖服务器上已有的数据。</div>';
      form.parentNode.insertBefore(banner, form);
      banner.addEventListener("click", function (event) {
        var action = event.target.dataset.draft;
        if (action === "restore") {
          apply(state);
          banner.remove();
        } else if (action === "discard") {
          sessionStorage.removeItem(key);
          banner.remove();
        }
      });
    }

    form.addEventListener("submit", function () {
      try {
        sessionStorage.removeItem(key);
      } catch (error) {
        /* ignore */
      }
    });

    document.addEventListener("keydown", function (event) {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        var button = form.querySelector('button[name="_action"][value="save"]');
        if (button && form.requestSubmit) form.requestSubmit(button);
        else form.submit();
      }
    });
  }

  /* ------------------------------------------------------------------ 启动 */

  document.addEventListener("DOMContentLoaded", function () {
    var productForm = document.querySelector('form[data-editor="product"]');
    if (productForm) {
      productForm.querySelectorAll(".tag-input").forEach(function (node) {
        node._tagInput = new TagInput(node);
      });
      setupDraft(productForm);
    }
    var generalForm = document.querySelector('form[data-editor="general"]');
    if (generalForm) {
      setupGeneralEditor(generalForm);
    }
    var cardForm = document.querySelector('form[data-editor="card"]');
    if (cardForm) {
      setupCardEditor(cardForm);
    }
  });
})();
