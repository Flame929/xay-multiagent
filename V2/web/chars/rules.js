// 监工（规则监工）：本项目原创角色，头像由 gpt-image-2 按现有 AI 娘画风生成（见 img/LICENSE.md）
(function () {
  'use strict';
  const img = face => '/xay-chars/img/rules-' + face + '.webp';
  const faces = ['calm', 'smile', 'serious', 'surprised', 'pout', 'eyesclosed', 'aggrieved', 'shy'];
  XayChars.register('rules', {
    name: '监工', color: '#14b8a6',
    images: Object.fromEntries(faces.map(face => [face, img(face)]))
  });
}());
