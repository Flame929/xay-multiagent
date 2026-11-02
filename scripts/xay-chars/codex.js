// GPT 娘·白龙娘：头像取自 ai-chan-sheets「设定图/02_GPT娘_白龙娘」的 8 种表情（CC BY-NC-SA 4.0，见 img/LICENSE.md）
(function () {
  'use strict';
  const img = face => '/xay-chars/img/codex-' + face + '.webp';
  const faces = ['calm', 'smile', 'serious', 'surprised', 'pout', 'eyesclosed', 'aggrieved', 'shy'];
  XayChars.register('codex', {
    name: 'GPT 娘·白龙娘', color: '#10a37f',
    images: Object.fromEntries(faces.map(face => [face, img(face)]))
  });
}());
