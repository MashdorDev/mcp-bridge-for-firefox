# Listing screenshots (addons.mozilla.org)

1280x800 slides built from real captures of the add-on: a headline plus
callouts on each. The source is `amo-slides.html` in the brag work folder,
rendered with `node amo-slides.mjs`. Upload in this order, with these captions
in each screenshot's description field on AMO:

1. `1-see-it-work.png`: Claude types into Wikipedia in a normal Firefox tab. The tab ring, its cursor, the status line and the glowing border show what it is doing.
2. `2-how-it-works.png`: Your AI client starts a small MCP server on your computer, which talks to this add-on. Nothing goes to the developer.
3. `3-real-input.png`: A real hover: Wikipedia opens its own link preview, and the status line says what the agent is checking.
4. `4-tabs-marked.png`: The toolbar popup lists every tab the agent is working in, with a Release button for each and a switch to pause it.
5. `5-permissions.png`: You choose what the agent may do. Run scripts in pages starts off, and pause stops every request at once.

The README shows the same images.
