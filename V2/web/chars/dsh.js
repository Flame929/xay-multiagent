// DeepSeek 娘·鲸鱼娘：头像取自 ai-chan-sheets「设定图/03_DeepSeek娘_鲸鱼娘」的 8 种表情（CC BY-NC-SA 4.0，见 img/LICENSE.md）
(function () {
  'use strict';
  const img = face => '/xay-chars/img/dsh-' + face + '.webp';
  const faces = ['calm', 'smile', 'serious', 'surprised', 'pout', 'eyesclosed', 'aggrieved', 'shy'];
  XayChars.register('dsh', {
    name: 'DeepSeek 娘·鲸鱼娘', color: '#4d6bfe',
    images: Object.fromEntries(faces.map(face => [face, img(face)]))
  });
}());
