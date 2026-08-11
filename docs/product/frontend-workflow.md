# Frontend work — how to keep the agent honest

An agent editing CSS cannot see the result, and an agent shown its own screenshot
will usually tell you it looks right. This page describes the loop that avoids
both problems. It needs no tools beyond the ones already listed in
[`browser.md`](browser.md) and [`visual.md`](visual.md).

## The principle

**The screenshot is for you. The measurement is for the model.**

An image persuades; a number refutes. Ask "does this look right?" and you get
agreement. Ask "is `scrollWidth` equal to `clientWidth` at 390px?" and you get a
fact that can come back false.

Use both: the capture so you can judge taste and layout, the measurement so the
model cannot talk itself into a pass.

## The loop

```
workspace_acquire(project)          # everything below needs the lease
browser_ensure()
browser_open("http://127.0.0.1:PORT/page")

visual_capture()                    # ← baseline, BEFORE any edit
                                    #   keep the artifact_id

fs_apply_patch(...)                 # make the change

browser_reload(ignore_cache=true)   # ← see the cache trap below
visual_capture()

visual_compare(before_id, after_id) # what actually moved, in pixels
browser_snapshot()                  # console and page errors
```

Capturing the baseline first is the step people skip. Without it you are
comparing the result against your memory of the page, which is exactly the kind
of judgement the loop exists to replace.

## The cache trap

Without `ignore_cache=true` you screenshot the **old stylesheet**. The page looks
unchanged, and the honest conclusion — "the edit did nothing" — is wrong. Worse,
the reverse happens too: a cached page that already looked fine reads as a pass.

If you hit this repeatedly in a real app, the durable fix is on the app's side:
version your static assets (`app.css?v=<mtime>`) so the browser cannot serve a
stale file at all.

## Measuring

There is deliberately no "evaluate this JavaScript" tool. Two routes cover the
ground, and the second is the one that lasts:

**`browser_snapshot`** — DOM highlights, accessibility tree, console and page
errors. Enough for presence and counts: *one figure, no leftover element, no
exception thrown*. A screenshot can look perfect while JavaScript is throwing;
this is where you catch that.

**A visual test in your own repo, run through `exec_run`** — for geometry and
computed style. A dozen lines of Playwright asserting `scrollWidth ==
clientWidth` across your viewports is worth more than any ad-hoc question,
because it runs again in CI six months from now. Mark it so it stays out of the
default test path if it needs a browser:

```python
@pytest.mark.visual
def test_no_horizontal_overflow(page):
    for width, height in ((1280, 800), (820, 1180), (390, 844)):
        page.set_viewport_size({"width": width, "height": height})
        assert page.evaluate("document.documentElement.scrollWidth") == width
```

Ad-hoc measurement disappears when the chat closes. A committed test does not.

## Giving it a target

If you already have a design, hand it over as an image before asking for code —
the client's vision model reads it, and "match this" beats three paragraphs of
description. If you do not, ask for a proposal and approve it before any CSS is
written; correcting a described layout is cheaper than correcting a built one.

Better still, put the reference **in the repo** with `fs_write_file` rather than
leaving it in the conversation. It survives the chat, the next session can look
at it, and it works for anything up to 2 MB — the mockup, but also the PDF spec,
the brand font or the logo the page needs. See
[`filesystem-binary-write.md`](filesystem-binary-write.md).

Either way, name the viewports you care about up front. `browser_set_viewport`
takes arbitrary sizes, and a layout that works at 1280 tells you nothing about
390.

## When it still goes wrong

| Symptom | Cause |
|---------|-------|
| Diff shows no change | Cached stylesheet — reload with `ignore_cache=true` |
| Looks right, behaves wrong | Check `browser_snapshot` for console errors |
| `visual_compare` refuses | Sizes differ; capture both at the same viewport |
| Passes on desktop, breaks on phone | Only one viewport was ever checked |
| Agent reports success you cannot see | It judged its own screenshot — ask for a number instead |
