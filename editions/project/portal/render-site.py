#!/usr/bin/env python3
"""Render the managed Project portal page (ADR 0002 §2.6).

    render-site.py SITE_MD CHAT_DOMAIN NOTE_DOMAIN
    render-site.py --default-site      (prints the starter site.md)

Prints index.html. The first line is the managed marker, so editions/project/install.sh
replaces the page on change and preserves a page an operator wrote by hand. Operator text
(introduction, contact, extra guidance) lives in SITE_MD (~/oaos/portal/site.md) and is
rendered from a small Markdown subset: '#'/'##'/'###' headings, '-' lists, paragraphs,
**bold** and [text](https://link). Everything else is escaped; raw HTML is not passed.
A missing SITE_MD renders the built-in introduction.
"""

import html
import re
import sys

MARKER = "<!-- OAOS Project managed portal -->"

DEFAULT_SITE = """# 소개

이 서버는 우리 팀의 **OAOS Project**입니다. 팀 채팅(Mattermost), 팀 위키(Outline), AI 에이전트 봇을 한곳에서 씁니다.

- 채팅방에서 에이전트 봇에게 조사·정리·문서 작성을 맡길 수 있습니다.
- 에이전트가 만든 문서는 Outline 위키에 에이전트 계정 이름으로 남습니다.

## 문의

운영자 연락처는 운영자가 `~/oaos/portal/site.md`에 적습니다.
"""

STYLE = ("body{font-family:system-ui,-apple-system,'Noto Sans KR',sans-serif;max-width:44rem;margin:2rem auto;padding:0 1rem;"
         "line-height:1.65;color:#1f2933}h1{margin-bottom:.2rem}.sub{color:#52606d;margin-top:0}section{margin:2rem 0}"
         ".links a,.apply{display:inline-block;margin:.3rem .6rem .3rem 0;padding:.55rem 1.1rem;border:1px solid #9aa5b1;"
         "border-radius:.4rem;text-decoration:none;color:#1f2933}.links a.apply,.apply{background:#1f2933;color:#fff;border-color:#1f2933}"
         "ol li{margin:.35rem 0}.en{color:#52606d;font-size:.95rem}footer{margin-top:3rem;color:#7b8794;font-size:.85rem}")


def inline(text):
    out, pos = [], 0
    for match in re.finditer(r"\*\*(.+?)\*\*|\[([^\]]+)\]\((https?://[^\s)]+)\)|`([^`]+)`", text):
        out.append(html.escape(text[pos:match.start()]))
        if match.group(1) is not None:
            out.append("<strong>%s</strong>" % html.escape(match.group(1)))
        elif match.group(2) is not None:
            out.append('<a href="%s" rel="noopener">%s</a>' % (html.escape(match.group(3), quote=True), html.escape(match.group(2))))
        else:
            out.append("<code>%s</code>" % html.escape(match.group(4)))
        pos = match.end()
    out.append(html.escape(text[pos:]))
    return "".join(out)


def markdown(source):
    blocks, paragraph, items = [], [], []

    def flush():
        if paragraph:
            blocks.append("<p>%s</p>" % inline(" ".join(paragraph)))
            paragraph.clear()
        if items:
            blocks.append("<ul>%s</ul>" % "".join("<li>%s</li>" % inline(item) for item in items))
            items.clear()

    for raw in source.splitlines():
        line = raw.rstrip()
        heading = re.match(r"^(#{1,3})\s+(.*)$", line)
        if heading:
            flush()
            level = len(heading.group(1)) + 1  # the page title is the only <h1>
            blocks.append("<h%d>%s</h%d>" % (level, inline(heading.group(2)), level))
        elif re.match(r"^\s*[-*]\s+", line):
            if paragraph:
                blocks.append("<p>%s</p>" % inline(" ".join(paragraph)))
                paragraph.clear()
            items.append(re.sub(r"^\s*[-*]\s+", "", line))
        elif not line.strip():
            flush()
        else:
            if items:
                flush()
            paragraph.append(line.strip())
    flush()
    return "\n".join(blocks)


def render(site, chat, note):
    chat_url, note_url = "https://" + chat, "https://" + note
    return """%s
<!doctype html>
<html lang="ko">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>OAOS Project</title><style>%s</style></head>
<body>
<h1>OAOS Project</h1>
<p class="sub">팀 채팅 · 팀 위키 · AI 에이전트 — 팀의 단일 입구 <span class="en">/ the team's front door</span></p>
<p class="links"><a href="%s">Mattermost (채팅 / chat)</a><a href="%s">Outline (위키 / wiki)</a><a class="apply" href="/apply">이용 신청 / Request access</a></p>
<section>
%s
</section>
<section>
<h2>사용 안내 <span class="en">/ Getting started</span></h2>
<ol>
<li><strong>이용 신청</strong> — <a href="/apply">신청서</a>를 내고 메일로 받은 6자리 코드를 입력합니다. <span class="en">Apply and enter the 6-digit code from your mail.</span></li>
<li><strong>승인과 가입</strong> — 관리자가 승인하면 Mattermost 초대 메일(또는 링크)로 가입하고 팀에 합류합니다. <span class="en">After approval, sign up through the Mattermost invitation and join the team.</span></li>
<li><strong>에이전트와 대화</strong> — 가입 몇 분 뒤부터 에이전트 봇에게 메시지를 보낼 수 있습니다. <span class="en">A few minutes after you sign up, the agent bot answers your messages.</span></li>
<li><strong>위키 사용</strong> — Outline 초대 메일로 합류해 팀 문서를 읽고 씁니다. <span class="en">Join Outline through its invitation and work on team documents.</span></li>
</ol>
<p class="en">이미 구성원이면 바로 <a href="%s">Mattermost</a>와 <a href="%s">Outline</a>에 로그인하세요. / Members sign in directly.</p>
</section>
<footer>Open Agent OS — Project edition</footer>
</body>
</html>""" % (MARKER, STYLE, html.escape(chat_url, quote=True), html.escape(note_url, quote=True), markdown(site),
              html.escape(chat_url, quote=True), html.escape(note_url, quote=True))


def main(argv):
    if len(argv) == 2 and argv[1] == "--default-site":
        sys.stdout.write(DEFAULT_SITE)
        return 0
    if len(argv) != 4 or not all(re.match(r"^[A-Za-z0-9.-]+$", value) for value in argv[2:]):
        sys.stderr.write("usage: render-site.py SITE_MD CHAT_DOMAIN NOTE_DOMAIN\n")
        return 2
    try:
        with open(argv[1], encoding="utf-8") as source:
            site = source.read()
    except OSError:
        site = DEFAULT_SITE
    sys.stdout.write(render(site, argv[2], argv[3]) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
