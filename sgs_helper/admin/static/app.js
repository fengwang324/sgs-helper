/* sgs-helper 本地管理页的少量交互。不引入任何前端框架。 */
(function () {
  "use strict";

  // 列表字段的候选按钮：把 id 追加到对应的 textarea（一行一项，去重）
  document.addEventListener("click", function (event) {
    var button = event.target.closest(".chip-btn");
    if (!button) return;
    event.preventDefault();
    var textarea = document.getElementById(button.dataset.target);
    if (!textarea) return;
    var value = button.dataset.value || "";
    var lines = textarea.value.split("\n").map(function (line) {
      return line.trim();
    }).filter(function (line) {
      return line.length > 0;
    });
    if (lines.indexOf(value) === -1) {
      lines.push(value);
    }
    textarea.value = lines.join("\n");
    textarea.focus();
  });

  // 表单里第一次改动就提示“离开会丢失修改”（只在编辑/新增页生效）
  var form = document.querySelector("form.entity-form");
  if (form) {
    var dirty = false;
    form.addEventListener("input", function () {
      dirty = true;
    });
    form.addEventListener("submit", function () {
      dirty = false;
    });
    window.addEventListener("beforeunload", function (event) {
      if (!dirty) return undefined;
      event.preventDefault();
      event.returnValue = "";
      return "";
    });
  }
})();
