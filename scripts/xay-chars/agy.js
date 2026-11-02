// Gemini 娘：头像取自 ai-chan-sheets「设定图/10_Gemini娘」的 8 种表情（CC BY-NC-SA 4.0，见 img/LICENSE.md）
(function () {
  'use strict';
  const img = face => '/xay-chars/img/agy-' + face + '.webp';
  const faces = ['calm', 'smile', 'serious', 'surprised', 'pout', 'eyesclosed', 'aggrieved', 'shy'];
  XayChars.register('agy', {
    name: 'Gemini 娘', color: '#7b6cf6',
    images: Object.fromEntries(faces.map(face => [face, img(face)]))
  });
}());
