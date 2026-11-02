// Claude 娘：头像取自 ai-chan-sheets「设定图/11_Claude娘_Anthropic」的 8 种表情（CC BY-NC-SA 4.0，见 img/LICENSE.md）
(function () {
  'use strict';
  const img = face => '/xay-chars/img/claude-' + face + '.webp';
  const faces = ['calm', 'smile', 'serious', 'surprised', 'pout', 'eyesclosed', 'aggrieved', 'shy'];
  XayChars.register('claude', {
    name: 'Claude 娘', color: '#d97757',
    images: Object.fromEntries(faces.map(face => [face, img(face)]))
  });
}());
