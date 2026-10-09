// Naver blog: collect the latest N posts → Markdown + JSONL
// Parser adapted from hubert-bioinformatics/naver-blog-archiver (MIT).
// Usage: BLOG_ID=<id> [COUNT=10] [OUT_DIR=path] node collect-latest.mjs
import fs from 'fs/promises';
import * as cheerio from 'cheerio';

const BLOG_ID = process.env.BLOG_ID;
const COUNT = Number(process.env.COUNT || 10);
if (!BLOG_ID) {
  console.error('BLOG_ID env var required. Example: BLOG_ID=exampleblog node collect-latest.mjs');
  process.exit(1);
}
const UA =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 ' +
  '(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function decodeTitle(raw) {
  if (!raw) return '';
  try {
    return decodeURIComponent(String(raw).replace(/\+/g, ' '));
  } catch {
    return String(raw).replace(/\+/g, ' ');
  }
}

function parsePostList(text) {
  try {
    return JSON.parse(text).postList;
  } catch {}
  try {
    const normalized = text
      .replace(/\\'/g, '\u0001')
      .replace(/'/g, '"')
      .replace(/([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:/g, '$1"$2":')
      .replace(/\u0001/g, "'");
    return JSON.parse(normalized).postList;
  } catch {}
  const out = [];
  const re =
    /logNo["']?\s*:\s*["'](\d+)["'][\s\S]{0,600}?title["']?\s*:\s*["']([^"']*)["'][\s\S]{0,300}?addDate["']?\s*:\s*["']([^"']*)["']/g;
  let m;
  while ((m = re.exec(text)) !== null) {
    out.push({ logNo: m[1], title: m[2], addDate: m[3] });
  }
  return out.length ? out : null;
}

async function fetchLatest() {
  const url =
    `https://blog.naver.com/PostTitleListAsync.naver?blogId=${BLOG_ID}` +
    `&countPerPage=${COUNT}&currentPage=1`;
  const res = await fetch(url, {
    headers: { 'User-Agent': UA, Referer: `https://blog.naver.com/${BLOG_ID}` },
  });
  const text = await res.text();
  const list = parsePostList(text);
  if (!list) throw new Error('post list parse failed: ' + text.slice(0, 200));
  return list.slice(0, COUNT).map((p) => ({
    logNo: String(p.logNo),
    listTitle: decodeTitle(p.title),
    addDate: p.addDate ?? null,
  }));
}

function cleanImageSrc(src) {
  return String(src || '').split('?')[0];
}

// mblogthumb is the downscaled CDN. postfiles + ?type=w3840 = original resolution (measured).
function originalUrl(src) {
  const s = String(src || '');
  if (!s) return '';
  if (s.includes('mblogthumb-phinf.pstatic.net')) {
    return s.replace('mblogthumb-phinf.pstatic.net', 'postfiles.pstatic.net') + '?type=w3840';
  }
  return s;
}

function renderSE($, root) {
  const out = [];
  const pushText = (t) => {
    t = String(t).replace(/\u200b/g, '').trim();
    if (t && out[out.length - 1] !== t) out.push(t);
  };
  const pushImage = (src) => {
    const s = originalUrl(cleanImageSrc(src));
    if (s) out.push(`![](${s})`);
  };

  const comps = root
    .find('.se-component')
    .filter((_, el) => $(el).parents('.se-component').length === 0);

  if (comps.length === 0) {
    // no components → legacy fallback (wrapper divs excluded to avoid duplicate output)
    root.find('.se-text-paragraph, p').each((_, el) => {
      const $el = $(el);
      if ($el.parents('.se-text-paragraph').length) return;
      pushText($el.text());
    });
    return out.join('\n\n');
  }

  comps.each((_, el) => {
    const $c = $(el);
    const imgs = $c.find('img.se-image-resource, img.__se_img_el, .se-image img');
    imgs.each((_, im) => {
      const src = $(im).attr('data-lazy-src') || $(im).attr('src');
      pushImage(src);
    });
    const iframeSrc = $c.find('iframe').first().attr('src');
    if (iframeSrc && /video|youtu|naver/i.test(iframeSrc)) {
      out.push(`[video] ${iframeSrc}`);
    }
    // In SE ONE the paragraph unit is .se-text-paragraph only.
    // Also matching the module wrapper (.se-module-text) duplicates every sentence.
    const $paras = $c.find('.se-text-paragraph');
    if ($paras.length) {
      $paras.each((_, p) => {
        const $p = $(p);
        if ($p.parents('.se-text-paragraph').length) return;
        pushText($p.text());
      });
    } else {
      pushText($c.text());
    }
  });

  return out.join('\n\n');
}

function renderLegacy($, root) {
  const out = [];
  const pushText = (t) => {
    t = String(t).replace(/\u200b/g, '').trim();
    if (t && out[out.length - 1] !== t) out.push(t);
  };
  root.find('p, img').each((_, el) => {
    const tag = el.tagName?.toLowerCase();
    if (tag === 'img') {
      const src = cleanImageSrc($(el).attr('src'));
      if (src) out.push(`![](${src})`);
    } else {
      pushText($(el).text());
    }
  });
  return out.join('\n\n');
}

function humanizeRelative(rel) {
  // Naver UI text (functional — matched against live pages): Korean relative timestamps.
  const m = String(rel || '').match(/(\d+)\s*(분|시간|일)\s*전/);
  if (!m) return null;
  const n = Number(m[1]);
  const unit = m[2];
  const ms = unit === '분' ? n * 60e3 : unit === '시간' ? n * 3600e3 : n * 86400e3;
  const d = new Date(Date.now() - ms + 9 * 3600 * 1000);
  const p = (x) => String(x).padStart(2, '0');
  return (
    `≈ ${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ` +
    `${p(d.getUTCHours())}:${p(d.getUTCMinutes())} KST (relative to collection time)`
  );
}

async function fetchPost(meta) {
  const res = await fetch(`https://m.blog.naver.com/${BLOG_ID}/${meta.logNo}`, {
    headers: { 'User-Agent': UA },
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const $ = cheerio.load(await res.text());

  const title =
    $('.se-title-text').first().text().trim() ||
    $('.se_title').first().text().trim() ||
    $('meta[property="og:title"]').attr('content')?.trim() ||
    meta.listTitle;

  const publishedAt =
    $('.se_publishDate').first().text().trim() ||
    $('.blog_date').first().text().trim() ||
    $('.se-documentTitle .date').first().text().trim() ||
    meta.addDate ||
    '';

  let contentMd = '';
  const seRoot = $('.se-main-container').first();
  if (seRoot.length) {
    contentMd = renderSE($, seRoot);
  } else {
    const legacy = $('#postViewArea, .post-view').first();
    if (legacy.length) contentMd = renderLegacy($, legacy);
    else contentMd = '';
  }

  const imageCount = (contentMd.match(/!\[\]\(/g) || []).length;
  const publishedAtApprox = humanizeRelative(publishedAt);
  return {
    ...meta,
    title,
    publishedAt,
    publishedAtApprox,
    contentMd,
    imageCount,
    url: `https://blog.naver.com/${BLOG_ID}/${meta.logNo}`,
  };
}

async function main() {
  const list = await fetchLatest();
  console.log(`list: ${list.length} posts`);
  const posts = [];
  for (const meta of list) {
    try {
      const p = await fetchPost(meta);
      posts.push(p);
      console.log(`✓ ${p.logNo} ${p.title.slice(0, 40)} (images ${p.imageCount})`);
    } catch (e) {
      console.error(`✗ ${meta.logNo} failed: ${e.message}`);
      posts.push({ ...meta, title: meta.listTitle, publishedAt: meta.addDate, contentMd: '', imageCount: 0, failed: true, url: `https://blog.naver.com/${BLOG_ID}/${meta.logNo}` });
    }
    await sleep(600);
  }

  const kst = new Date(Date.now() + 9 * 3600 * 1000);
  const stamp = kst.toISOString().slice(0, 16).replace('T', ' ');
  const dateOnly = kst.toISOString().slice(0, 10);

  const lines = [];
  lines.push(`# ${BLOG_ID} — latest ${posts.length} posts`);
  lines.push('');
  lines.push(`- Collected: ${stamp} KST`);
  lines.push(`- Source: https://m.blog.naver.com/PostList.naver?blogId=${BLOG_ID}&tab=1`);
  lines.push(`- Method: unofficial collection of Naver blog public list/post pages; parser based on naver-blog-archiver; images: original-resolution URLs`);
  lines.push('');
  posts.forEach((p, i) => {
    lines.push('---');
    lines.push('');
    lines.push(`## ${i + 1}. ${p.title}`);
    lines.push('');
    const when = p.publishedAt ? (p.publishedAtApprox ? `${p.publishedAt} · ${p.publishedAtApprox}` : p.publishedAt) : 'unknown';
    lines.push(`- Published: ${when} · Original: ${p.url}`);
    if (p.imageCount) lines.push(`- Images: ${p.imageCount}`);
    if (p.failed) lines.push(`- ⚠ body fetch failed`);
    lines.push('');
    lines.push(p.contentMd || '_(no body)_');
    lines.push('');
  });
  const md = lines.join('\n');

  const dir = process.env.OUT_DIR || `${process.env.HOME || '.'}/data/naver-blog/${BLOG_ID}`;
  await fs.mkdir(dir, { recursive: true });
  const mdPath = `${dir}/${BLOG_ID}-latest${posts.length}-${dateOnly}.md`;
  const jsonlPath = `${dir}/${BLOG_ID}-latest${posts.length}-${dateOnly}.jsonl`;
  await fs.writeFile(mdPath, md, 'utf-8');
  await fs.writeFile(
    jsonlPath,
    posts.map((p) => JSON.stringify({ logNo: p.logNo, url: p.url, title: p.title, publishedAt: p.publishedAt, publishedAtApprox: p.publishedAtApprox ?? null, images: (p.contentMd.match(/!\[\]\(([^)]+)\)/g) || []).map((s) => s.slice(4, -1)), content: p.contentMd, fetchedAt: new Date().toISOString() })).join('\n'),
    'utf-8'
  );
  console.log(`md: ${mdPath}`);
  console.log(`jsonl: ${jsonlPath}`);
}

main().catch((e) => {
  console.error('failed:', e.message);
  process.exit(1);
});
