/* ============================================================================
   卡图上传助手（本地 Wiki / 管理页共用）
   ----------------------------------------------------------------------------
   用法（模板里）：
     <button type="button" class="btn js-upload-image"
             data-upload-target="image"
             data-upload-preview="image-preview">上传图片</button>
   点它 -> 选文件 -> POST /api/upload-image -> 把返回的**文件名**写进目标字段，
   并刷新预览。<input type="file"> 是点击时临时创建的，不参与表单提交
   （卡图名由目标字段提交，与 Card.image —— 相对 data/card_images/ 的文件名 —— 一致）。
   ========================================================================== */
(function () {
  'use strict';

  function findField(name) {
    return document.querySelector('input[name="' + name + '"]');
  }

  function setPreview(id, url, alt) {
    var box = document.getElementById(id);
    if (!box) return;
    box.innerHTML = '';
    if (!url) return;
    var img = document.createElement('img');
    img.src = url;
    img.alt = alt || '';
    img.style.maxWidth = '150px';
    img.style.maxHeight = '210px';
    img.style.border = '1px solid #e5e7eb';
    img.style.borderRadius = '8px';
    img.style.display = 'block';
    img.style.marginTop = '8px';
    box.appendChild(img);
  }

  function upload(file, target, previewId, button) {
    var original = button.textContent;
    button.disabled = true;
    button.textContent = '上传中…';

    var body = new FormData();
    body.append('file', file);

    fetch('/api/upload-image', { method: 'POST', body: body })
      .then(function (res) {
        return res.json().then(function (data) {
          return { ok: res.ok, data: data };
        });
      })
      .then(function (result) {
        if (!result.ok) {
          throw new Error((result.data && result.data.detail) || '上传失败');
        }
        var field = findField(target);
        if (field) field.value = result.data.file;
        // 组合框（editor.js）的可见输入框也要同步
        var combo = document.querySelector('.combo[data-combo="' + target + '"] .combo-input');
        if (combo) combo.value = result.data.file;
        setPreview(previewId, result.data.url, result.data.file);
        button.textContent = '已上传：' + result.data.file;
        window.setTimeout(function () {
          button.textContent = original;
          button.disabled = false;
        }, 2200);
      })
      .catch(function (err) {
        button.textContent = original;
        button.disabled = false;
        window.alert('上传失败：' + err.message);
      });
  }

  document.addEventListener('click', function (event) {
    var button = event.target.closest('.js-upload-image');
    if (!button) return;
    event.preventDefault();

    var target = button.getAttribute('data-upload-target') || 'image';
    var previewId = button.getAttribute('data-upload-preview') || '';

    var picker = document.createElement('input');
    picker.type = 'file';
    picker.accept = '.jpg,.jpeg,.png,.webp,image/jpeg,image/png,image/webp';
    picker.addEventListener('change', function () {
      if (picker.files && picker.files[0]) {
        upload(picker.files[0], target, previewId, button);
      }
    });
    picker.click();
  });
})();
