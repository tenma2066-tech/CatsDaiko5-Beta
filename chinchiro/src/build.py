# src の各ファイルを1つの index.html（1つ上のフォルダ）にまとめる
import os
here = os.path.dirname(os.path.abspath(__file__))
h = open(os.path.join(here, 'index.html'), encoding='utf-8').read()
for tag, f in [('PHYSICS', 'physics.js'), ('GFX', 'gfx.js'), ('AUDIO', 'audio.js'), ('GAME', 'game.js')]:
    js = open(os.path.join(here, f), encoding='utf-8').read().replace("if (typeof module !== 'undefined') module.exports = PHYS;", "")
    h = h.replace('/*%s*/' % tag, js)
open(os.path.join(here, '..', 'index.html'), 'w', encoding='utf-8').write(h)
print('index.html を作りました')
