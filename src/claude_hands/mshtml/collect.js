// Runs inside the page. Written for the oldest document mode an IE control
// still uses (IE5 quirks, which is what an MFC host gets by default): no JSON,
// no Array.prototype.forEach, no String.prototype.trim.
//
// One call returns every element worth listing. Walking the DOM from outside
// costs a cross-process round trip per property, and a form page has hundreds
// of elements with half a dozen properties each.
//
// Records are separated by \x1e and fields by \x1f; both are stripped from
// the text that goes in them.
(function (maxNodes) {
    var RS = '\x1e', US = '\x1f';
    var out = [], consumed = [], count = 0;
    var all = document.all;
    var zoom = 1;
    try {
        if (screen.deviceXDPI && screen.logicalXDPI) zoom = screen.deviceXDPI / screen.logicalXDPI;
    } catch (e) {}

    function clean(s) {
        if (s === null || s === undefined) return '';
        s = String(s).replace(/[\x1e\x1f\s]+/g, ' ').replace(/^ +| +$/g, '');
        return s.length > 200 ? s.substring(0, 200) : s;
    }
    function lower(s) { return s ? String(s).toLowerCase() : ''; }
    function shown(el) {
        try { return !(el.offsetWidth === 0 && el.offsetHeight === 0); } catch (e) { return true; }
    }
    function used(node) {
        for (var i = 0; i < consumed.length; i++) if (consumed[i] === node) return true;
        return false;
    }
    function textOf(node) {
        if (!node) return '';
        if (node.nodeType === 3) return clean(node.nodeValue);
        if (node.nodeType === 1) return clean(node.innerText);
        return '';
    }
    function isField(node) {
        return node && node.nodeType === 1 &&
            (node.tagName === 'INPUT' || node.tagName === 'SELECT' || node.tagName === 'TEXTAREA');
    }

    // What a person reading the form would call this control. Old pages rarely
    // use <label>: a checkbox is followed by its word, a text box sits in the
    // cell after its caption.
    function labelOf(el) {
        var i, n, t, type = lower(el.type);
        if (el.id) {
            var labels = document.getElementsByTagName('label');
            for (i = 0; i < labels.length; i++) {
                if (labels[i].htmlFor === el.id) { consumed.push(labels[i]); return textOf(labels[i]); }
            }
        }
        for (n = el.parentNode; n && n.tagName; n = n.parentNode) {
            if (n.tagName === 'LABEL') { consumed.push(n); return textOf(n); }
        }
        if (type === 'checkbox' || type === 'radio') {
            for (n = el.nextSibling; n; n = n.nextSibling) {
                if (isField(n)) break;
                t = textOf(n);
                if (t) { consumed.push(n); return t; }
            }
        }
        if (el.title) return clean(el.title);
        for (n = el.previousSibling; n; n = n.previousSibling) {
            if (isField(n)) break;
            t = textOf(n);
            if (t) { consumed.push(n); return t; }
        }
        var cell = el.parentNode;
        if (cell && (cell.tagName === 'TD' || cell.tagName === 'TH')) {
            for (n = cell.previousSibling; n && n.nodeType !== 1; n = n.previousSibling) {}
            if (n && !n.getElementsByTagName('input').length) {
                t = textOf(n);
                if (t) { consumed.push(n); return t; }
            }
        }
        return '';
    }

    // A styled checkbox: the real <input> is shrunk to nothing and a
    // <label for=id> drawn as a picture takes the clicks. K-에듀파인's 기안
    // window does this for every radio and checkbox in its side panel, and
    // judged by the input's own size they all looked invisible.
    function standIn(el) {
        if (!el.id) return null;
        var labels = document.getElementsByTagName('label');
        for (var i = 0; i < labels.length; i++) {
            if (labels[i].htmlFor === el.id && shown(labels[i])) return labels[i];
        }
        return null;
    }

    function rectOf(el) {
        try {
            var r = el.getBoundingClientRect();
            return [Math.round(r.left * zoom), Math.round(r.top * zoom),
                    Math.round((r.right - r.left) * zoom), Math.round((r.bottom - r.top) * zoom)].join(',');
        } catch (e) { return ''; }
    }

    // `box` is what is on screen in the element's place, when that is not the
    // element itself.
    function emit(index, parent, role, el, name, value, state, box) {
        if (count >= maxNodes) return;
        count++;
        var disabled = el.disabled ? '1' : '0';
        var readonly = el.readOnly ? '1' : '0';
        out.push([index, parent, role, el.tagName, lower(el.type), clean(el.id), clean(el.name),
                  clean(name), clean(value), state, disabled, readonly, rectOf(box || el)].join(US));
    }

    var TEXT_TAGS = {H1:1, H2:1, H3:1, H4:1, H5:1, H6:1, P:1, TD:1, TH:1, LI:1, SPAN:1, DIV:1,
                     B:1, STRONG:1, FONT:1, CAPTION:1, LEGEND:1, DT:1, DD:1, LABEL:1, PRE:1, CENTER:1};

    // Controls first, so the words they consume as labels are known before
    // the plain text is listed.
    var texts = [];
    for (var i = 0; i < all.length && count < maxNodes; i++) {
        var el = all[i], tag = el.tagName, type = lower(el.type), role = '';
        if (!tag || tag === '!') continue;
        if (tag === 'IFRAME' || tag === 'FRAME') {
            emit(i, -1, 'frame', el, el.name || el.id || el.src, '', '');
            continue;
        }
        var box = null;
        if (!shown(el)) {
            if (tag === 'INPUT' && (type === 'checkbox' || type === 'radio')) box = standIn(el);
            if (!box) continue;
        }
        if (tag === 'INPUT') {
            if (type === 'hidden') continue;
            if (type === 'checkbox') role = 'checkbox';
            else if (type === 'radio') role = 'radiobutton';
            else if (type === 'button' || type === 'submit' || type === 'reset' || type === 'image') role = 'button';
            else if (type === 'file') role = 'button';
            else role = 'edit';
            if (role === 'button') emit(i, -1, role, el, el.value || el.alt || el.title || el.id, '', '');
            else if (role === 'edit') emit(i, -1, role, el, labelOf(el), type === 'password' ? '' : el.value, type === 'password' ? 'secret' : '');
            else emit(i, -1, role, el, labelOf(el) || (box && clean(box.title)), '', el.checked ? 'on' : 'off', box);
        } else if (tag === 'TEXTAREA') {
            emit(i, -1, 'edit', el, labelOf(el), el.value, '');
        } else if (tag === 'SELECT') {
            var chosen = el.selectedIndex >= 0 ? el.options[el.selectedIndex] : null;
            emit(i, -1, 'combobox', el, labelOf(el), chosen ? chosen.text : '', '');
            for (var k = 0; k < el.options.length && k < 50; k++) {
                var opt = el.options[k];
                emit(opt.sourceIndex, i, 'listitem', opt, opt.text, opt.value, opt.selected ? 'selected' : '');
            }
        } else if (tag === 'BUTTON') {
            emit(i, -1, 'button', el, textOf(el) || el.title || el.id, '', '');
        } else if (tag === 'A') {
            var linkText = textOf(el);
            if (!linkText) {
                var img = el.getElementsByTagName('img');
                linkText = img.length ? clean(img[0].alt || img[0].title) : clean(el.title);
            }
            // An image link with no words still goes somewhere when pressed.
            if (linkText || el.onclick || el.href) emit(i, -1, 'hyperlink', el, linkText || el.id, '', '');
        } else if (tag === 'OBJECT' || tag === 'EMBED') {
            emit(i, -1, 'activex', el, el.id || el.name, lower(el.classid), '');
        } else if (el.onclick && (tag === 'IMG' || tag === 'SPAN' || tag === 'TD' || tag === 'DIV' || tag === 'LI')) {
            emit(i, -1, 'button', el, textOf(el) || el.alt || el.title || el.id, '', '');
        } else if (TEXT_TAGS[tag]) {
            texts.push(i);
        }
    }

    // Words inside a link or button are its caption: clicking them presses
    // it. Words in a plain table cell press nothing.
    //
    // A handler attached with jQuery or attachEvent leaves no onclick property
    // to see, so a pointer cursor counts too — that is how such a page tells
    // a person "this presses". Table rows and cells are left out: a row you
    // click to select shows the same cursor over its 결재 column, and lesson
    // 34's false refusal came from there.
    var ROW_PARTS = {TR:1, TD:1, TH:1, TBODY:1, THEAD:1, TABLE:1};
    function pointing(n) {
        try {
            var cursor = n.currentStyle ? n.currentStyle.cursor : '';
            return !ROW_PARTS[n.tagName] && (cursor === 'pointer' || cursor === 'hand');
        } catch (e) { return false; }
    }
    function pressable(n) {
        for (; n && n.tagName && n.tagName !== 'BODY'; n = n.parentNode) {
            if (n.tagName === 'A' || n.tagName === 'BUTTON' || n.onclick || pointing(n)) return true;
        }
        return false;
    }

    for (var j = 0; j < texts.length && count < maxNodes; j++) {
        var holder = all[texts[j]], words = [];
        for (var c = holder.firstChild; c; c = c.nextSibling) {
            if (c.nodeType === 3 && !used(c) && !used(holder)) {
                var w = clean(c.nodeValue);
                if (w) words.push(w);
            }
        }
        if (words.length) emit(texts[j], -1, 'text', holder, words.join(' '), '', pressable(holder) ? 'press' : '');
    }

    out.unshift([clean(document.title), document.documentMode || '', zoom, count >= maxNodes ? '1' : '0'].join(US));
    return out.join(RS);
})
