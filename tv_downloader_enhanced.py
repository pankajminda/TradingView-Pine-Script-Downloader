#!/usr/bin/env python3
"""
TradingView Pine Script Downloader (Enhanced Version)
======================================================
More robust source code extraction with multiple fallback methods.

This version includes:
- Multiple extraction strategies
- Better error handling
- Cookie consent handling
- Progress saving/resuming
- JSON export of metadata
"""

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urljoin
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


def sanitize_filename(name: str) -> str:
    """Convert a string to a valid filename."""
    name = re.sub(r'[<>:"/\\|?*\[\]]', '', name)
    name = re.sub(r'\s+', '_', name)
    name = name.strip('._')
    return name[:200] if len(name) > 200 else name or "unnamed_script"


def extract_script_id(url: str) -> str:
    """Extract script ID from TradingView URL."""
    match = re.search(r'/script/([^-/]+)', url)
    return match.group(1) if match else ""


class EnhancedTVScraper:
    def __init__(self, output_dir: str = "./pinescript_downloads", headless: bool = True):
        self.output_dir = Path(output_dir)
        self.headless = headless
        self.browser = None
        self.context = None
        self.page = None
        self.stats = {
            'downloaded': 0,
            'skipped_protected': 0,
            'skipped_no_code': 0,
            'failed': 0,
            'total': 0
        }
        self.results = []
        self.progress_file = None
        
    async def setup(self):
        """Initialize the browser with optimized settings."""
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=self.headless,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-dev-shm-usage'
            ]
        )
        self.context = await self.browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            locale='en-US'
        )
        self.page = await self.context.new_page()
        
        # Handle cookie consent popups
        self.page.on('dialog', lambda dialog: dialog.accept())
        
    async def cleanup(self):
        """Close browser and cleanup."""
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def handle_cookie_consent(self):
        """Click away cookie consent banners if present."""
        try:
            consent_selectors = [
                'button:has-text("Accept")',
                'button:has-text("Accept All")',
                'button:has-text("I agree")',
                '[class*="cookie"] button',
                '[class*="consent"] button'
            ]
            for selector in consent_selectors:
                try:
                    btn = self.page.locator(selector)
                    if await btn.count() > 0:
                        await btn.first.click()
                        await self.page.wait_for_timeout(500)
                        break
                except:
                    continue
        except:
            pass

    async def get_scripts_from_listing(self, max_scroll_attempts: int = 20) -> list[dict]:
        """Get all scripts by scrolling and clicking 'load more'."""
        scripts = {}
        last_count = 0
        no_change_count = 0
        
        for attempt in range(max_scroll_attempts):
            # Get current scripts
            current_scripts = await self.page.evaluate('''() => {
                const scripts = [];
                const links = document.querySelectorAll('a[href*="/script/"]');
                
                links.forEach(link => {
                    const href = link.href;
                    // Filter to only main script links (not comments, etc.)
                    if (href && href.match(/\\/script\\/[a-zA-Z0-9]+-.+\\/?$/)) {
                        const title = link.textContent?.trim();
                        if (title && title.length > 5 && !scripts.some(s => s.url === href)) {
                            scripts.push({
                                url: href,
                                title: title.substring(0, 200)
                            });
                        }
                    }
                });
                
                return scripts;
            }''')
            
            # Add to collection
            for s in current_scripts:
                if s['url'] not in scripts:
                    scripts[s['url']] = s
            
            # Check if we got new scripts
            if len(scripts) == last_count:
                no_change_count += 1
                if no_change_count >= 3:
                    break
            else:
                no_change_count = 0
            
            last_count = len(scripts)
            print(f"   Found {len(scripts)} scripts... (attempt {attempt + 1})", end='\r')
            
            # Try to load more
            try:
                load_more = self.page.locator('button:has-text("Show more")')
                if await load_more.count() > 0:
                    await load_more.first.click()
                    await self.page.wait_for_timeout(2000)
                else:
                    # Try scrolling instead
                    await self.page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                    await self.page.wait_for_timeout(1500)
            except:
                await self.page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                await self.page.wait_for_timeout(1500)
        
        print()  # New line after progress
        return list(scripts.values())

    async def extract_pine_source(self, script_url: str) -> dict:
        """
        Extract Pine Script source code using multiple strategies.
        Returns dict with: source_code, title, version, is_strategy, error
        """
        result = {
            'url': script_url,
            'script_id': extract_script_id(script_url),
            'title': '',
            'source_code': '',
            'version': '',
            'is_strategy': False,
            'is_protected': False,
            'author': '',
            'published_date': '',
            'description': '',
            'tags': [],
            'boosts': 0,
            'views': 0,
            'comments': 0,
            'error': None
        }
        
        try:
            response = await self.page.goto(script_url, wait_until='domcontentloaded', timeout=30000)
            if not response or response.status >= 400:
                result['error'] = f"HTTP {response.status if response else 'No response'}"
                return result
                
            await self.page.wait_for_timeout(2000)
            await self.handle_cookie_consent()
            
            # Extract metadata
            result['title'] = await self.page.evaluate('''() => {
                const h1 = document.querySelector('h1');
                return h1 ? h1.textContent.trim() : '';
            }''')
            
            result['author'] = await self.page.evaluate('''() => {
                const authorLink = document.querySelector('a[href^="/u/"]');
                return authorLink ? authorLink.textContent.trim().replace('by ', '') : '';
            }''')

            # Extract extended metadata (published date, description, tags, stats)
            extended_meta = await self.page.evaluate('''() => {
                const meta = {
                    published_date: '',
                    description: '',
                    tags: [],
                    boosts: 0,
                    views: 0,
                    comments: 0
                };

                // Published date from time element
                const timeEl = document.querySelector('time');
                if (timeEl) {
                    meta.published_date = timeEl.getAttribute('datetime') || timeEl.textContent.trim();
                }

                // Description from meta tag
                const metaDesc = document.querySelector('meta[name="description"]');
                if (metaDesc) {
                    meta.description = metaDesc.getAttribute('content') || '';
                }

                // Tags from section with tags class
                const tagSection = document.querySelector('section[class*="tags"]');
                if (tagSection) {
                    const tagLinks = tagSection.querySelectorAll('a[href*="/scripts/"]');
                    tagLinks.forEach(a => {
                        const tagName = a.textContent.trim();
                        if (tagName && !meta.tags.includes(tagName)) {
                            meta.tags.push(tagName);
                        }
                    });
                }

                // Boosts from aria-label (e.g., "836 boosts")
                const boostSpan = document.querySelector('span[aria-label*="boosts"]');
                if (boostSpan) {
                    const label = boostSpan.getAttribute('aria-label') || '';
                    const match = label.match(/(\\d+)/);
                    if (match) meta.boosts = parseInt(match[1], 10);
                }

                // Views/Uses from aria-label
                const viewsSpan = document.querySelector('span[aria-label*="uses"]');
                if (viewsSpan) {
                    const label = viewsSpan.getAttribute('aria-label') || '';
                    const match = label.match(/(\\d+)/);
                    if (match) meta.views = parseInt(match[1], 10);
                }

                // Comments from aria-label
                const commentsSpan = document.querySelector('span[aria-label*="comments"]');
                if (commentsSpan) {
                    const label = commentsSpan.getAttribute('aria-label') || '';
                    const match = label.match(/(\\d+)/);
                    if (match) meta.comments = parseInt(match[1], 10);
                }

                return meta;
            }''')

            # Merge extended metadata
            result['published_date'] = extended_meta.get('published_date', '')
            result['description'] = extended_meta.get('description', '')
            result['tags'] = extended_meta.get('tags', [])
            result['boosts'] = extended_meta.get('boosts', 0)
            result['views'] = extended_meta.get('views', 0)
            result['comments'] = extended_meta.get('comments', 0)

            # Check if open-source (FIXED: look for explicit open-source indicator, not lock icons)
            script_type = await self.page.evaluate('''() => {
                const pageText = document.body.innerText;
                const pageUpper = pageText.toUpperCase();
                
                // Check for explicit OPEN-SOURCE indicator
                const isOpenSource = pageUpper.includes('OPEN-SOURCE SCRIPT') || 
                                    pageUpper.includes('OPEN-SOURCE') ||
                                    pageText.includes('Open-source script');
                
                // Check for invite-only or protected (these override open-source)
                const isInviteOnly = pageText.toLowerCase().includes('invite-only');
                const isProtected = pageText.toLowerCase().includes('protected script');
                
                return {
                    isOpenSource: isOpenSource && !isInviteOnly && !isProtected,
                    isInviteOnly,
                    isProtected
                };
            }''')
            
            if not script_type['isOpenSource']:
                result['is_protected'] = True
                if script_type['isInviteOnly']:
                    result['error'] = 'invite-only'
                elif script_type['isProtected']:
                    result['error'] = 'protected'
                else:
                    result['error'] = 'not open-source'
                return result
            
            # Strategy 1: Click Source Code tab and extract
            source_code = await self._try_source_tab_extraction()
            
            # Strategy 2: Look for code in page directly
            if not source_code:
                source_code = await self._try_direct_extraction()
            
            # Strategy 3: Check for embedded script data
            if not source_code:
                source_code = await self._try_embedded_extraction()
            
            if source_code:
                result['source_code'] = source_code.strip()
                # Detect version and type
                version_match = re.search(r'//@version=(\d+)', source_code)
                result['version'] = version_match.group(1) if version_match else ''
                result['is_strategy'] = 'strategy(' in source_code
            
            return result
            
        except PlaywrightTimeoutError:
            result['error'] = 'Timeout'
            return result
        except Exception as e:
            result['error'] = str(e)[:100]
            return result

    async def _try_source_tab_extraction(self) -> str:
        """Try clicking Source Code tab and extracting."""
        try:
            # Find and click Source Code tab
            tab_selectors = [
                '[role="tab"]:has-text("Source code")',
                'button:has-text("Source code")',
                'div:has-text("Source code"):not(:has(*))',
            ]
            
            for selector in tab_selectors:
                try:
                    tab = self.page.locator(selector)
                    if await tab.count() > 0:
                        await tab.first.click()
                        await self.page.wait_for_timeout(2500)
                        break
                except:
                    continue
            
            # Extract code - FIXED: Look for container with many child divs (line-by-line code)
            code = await self.page.evaluate('''() => {
                // Find all divs and look for containers with many child divs
                const allDivs = document.querySelectorAll('div');
                
                for (const container of allDivs) {
                    const children = Array.from(container.children);
                    
                    // If this div has many child divs (50+), it might be the code container
                    if (children.length > 50) {
                        const texts = children.map(c => c.textContent?.trim() || '');
                        const joined = texts.join('\\n');
                        
                        // Check if this looks like Pine Script
                        if (joined.includes('//@version') && 
                            (joined.includes('indicator(') || joined.includes('strategy('))) {
                            // Filter out line numbers (pure numeric lines)
                            const codeLines = texts.filter(t => t && !/^\\d+$/.test(t));
                            return codeLines.join('\\n');
                        }
                    }
                }
                
                // Fallback: Look for pre/code elements
                const codeElements = document.querySelectorAll('pre code, pre');
                for (const elem of codeElements) {
                    const text = elem.textContent || '';
                    if (text.includes('//@version') && text.length > 200) {
                        return text;
                    }
                }
                
                return '';
            }''')
            
            return code
        except:
            return ''

    async def _try_direct_extraction(self) -> str:
        """Try extracting code directly from page elements."""
        try:
            return await self.page.evaluate('''() => {
                // Method 1: Look for containers with many child divs (line-by-line code)
                const allDivs = document.querySelectorAll('div');
                
                for (const container of allDivs) {
                    const children = Array.from(container.children);
                    
                    if (children.length > 50) {
                        const texts = children.map(c => c.textContent?.trim() || '');
                        const joined = texts.join('\\n');
                        
                        if (joined.includes('//@version') && 
                            (joined.includes('indicator(') || joined.includes('strategy('))) {
                            const codeLines = texts.filter(t => t && !/^\\d+$/.test(t));
                            return codeLines.join('\\n');
                        }
                    }
                }
                
                // Method 2: Look for any pre/code element with Pine Script content
                const codeElements = document.querySelectorAll('pre, code, [class*="source"]');
                
                for (const elem of codeElements) {
                    const text = elem.textContent || '';
                    if (text.length > 100 && 
                        (text.includes('//@version') || 
                         text.includes('indicator(') || 
                         text.includes('strategy(') ||
                         text.includes('plot('))) {
                        return text;
                    }
                }
                return '';
            }''')
        except:
            return ''

    async def _try_embedded_extraction(self) -> str:
        """Try extracting code from embedded page data."""
        try:
            return await self.page.evaluate('''() => {
                // Check for script data in page scripts
                const scripts = document.querySelectorAll('script');
                for (const script of scripts) {
                    const content = script.textContent || '';
                    // Look for Pine Script patterns in JSON data
                    const match = content.match(/"source"\\s*:\\s*"([^"]+)"/);
                    if (match) {
                        const decoded = match[1]
                            .replace(/\\\\n/g, '\\n')
                            .replace(/\\\\t/g, '\\t')
                            .replace(/\\\\"/g, '"');
                        if (decoded.includes('//@version') || decoded.includes('indicator(')) {
                            return decoded;
                        }
                    }
                }
                return '';
            }''')
        except:
            return ''

    def save_script(self, result: dict, category: str) -> Path:
        """Save Pine Script to file with metadata."""
        category_dir = self.output_dir / sanitize_filename(category)
        category_dir.mkdir(parents=True, exist_ok=True)
        
        # Create filename
        safe_title = sanitize_filename(result['title'] or 'unknown')
        filename = f"{result['script_id']}_{safe_title}.pine"
        filepath = category_dir / filename
        
        # Format tags for header
        tags_str = ', '.join(result.get('tags', [])) if result.get('tags') else ''

        # Build header with extended metadata
        header = [
            f"// Title: {result['title']}",
            f"// Script ID: {result['script_id']}",
            f"// Author: {result['author']}",
            f"// URL: {result['url']}",
            f"// Published: {result.get('published_date', '')}",
            f"// Downloaded: {datetime.now().isoformat()}",
            f"// Pine Version: {result['version']}",
            f"// Type: {'Strategy' if result['is_strategy'] else 'Indicator'}",
            f"// Boosts: {result.get('boosts', 0)}",
            f"// Views: {result.get('views', 0)}",
            f"// Tags: {tags_str}",
            "//",
            ""
        ]
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write('\n'.join(header))
            f.write(result['source_code'])
        
        return filepath

    def save_progress(self, category: str):
        """Save progress to JSON for resuming."""
        progress_path = self.output_dir / sanitize_filename(category) / '.progress.json'
        progress_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(progress_path, 'w') as f:
            json.dump({
                'stats': self.stats,
                'results': self.results,
                'timestamp': datetime.now().isoformat()
            }, f, indent=2)

    def load_progress(self, category: str) -> set:
        """Load previous progress. Returns set of completed URLs."""
        progress_path = self.output_dir / sanitize_filename(category) / '.progress.json'
        if progress_path.exists():
            try:
                with open(progress_path) as f:
                    data = json.load(f)
                    return {r['url'] for r in data.get('results', [])}
            except:
                pass
        return set()

    async def download_all(self, base_url: str, max_pages: int = 20, 
                          delay: float = 2.0, resume: bool = True):
        """Main download method."""
        # Determine category
        parsed = urlparse(base_url)
        path_parts = [p for p in parsed.path.strip('/').split('/') if p]
        category = path_parts[-1] if path_parts else "scripts"
        
        print(f"\n{'='*70}")
        print(f"  TradingView Pine Script Downloader")
        print(f"{'='*70}")
        print(f"  URL: {base_url}")
        print(f"  Category: {category}")
        print(f"  Output: {self.output_dir}")
        print(f"{'='*70}\n")
        
        await self.setup()
        
        try:
            # Load previous progress
            completed_urls = self.load_progress(category) if resume else set()
            if completed_urls:
                print(f"📂 Resuming: {len(completed_urls)} scripts already processed\n")
            
            # Navigate and collect scripts
            print("📋 Collecting script list...")
            await self.page.goto(base_url, wait_until='networkidle', timeout=60000)
            await self.page.wait_for_timeout(2000)
            await self.handle_cookie_consent()
            
            scripts = await self.get_scripts_from_listing(max_pages)
            self.stats['total'] = len(scripts)
            
            # Filter already completed
            scripts = [s for s in scripts if s['url'] not in completed_urls]
            print(f"✓ Found {self.stats['total']} scripts, {len(scripts)} to process\n")
            
            if not scripts:
                print("Nothing new to download!")
                return
            
            # Process each script
            print(f"{'='*70}")
            print(f"  Downloading...")
            print(f"{'='*70}\n")
            
            for i, script_info in enumerate(scripts, 1):
                url = script_info['url']
                title = script_info.get('title', 'Unknown')[:50]
                
                print(f"[{i}/{len(scripts)}] {title}...")
                
                result = await self.extract_pine_source(url)
                self.results.append(result)
                
                if result['is_protected']:
                    print(f"         ⊘ Protected/Invite-only")
                    self.stats['skipped_protected'] += 1
                elif result['error']:
                    print(f"         ✗ Error: {result['error']}")
                    self.stats['failed'] += 1
                elif result['source_code']:
                    filepath = self.save_script(result, category)
                    print(f"         ✓ Saved ({len(result['source_code'])} chars)")
                    self.stats['downloaded'] += 1
                else:
                    print(f"         ⊘ No source code found")
                    self.stats['skipped_no_code'] += 1
                
                # Save progress periodically
                if i % 10 == 0:
                    self.save_progress(category)
                
                # Delay between requests
                if i < len(scripts):
                    await self.page.wait_for_timeout(int(delay * 1000))
            
            # Final progress save
            self.save_progress(category)
            
            # Export metadata
            self._export_metadata(category)
            
            # Print summary
            self._print_summary(category)
            
        finally:
            await self.cleanup()

    def _export_metadata(self, category: str):
        """Export all metadata to JSON."""
        metadata_path = self.output_dir / sanitize_filename(category) / 'metadata.json'
        
        export_data = {
            'download_date': datetime.now().isoformat(),
            'category': category,
            'statistics': self.stats,
            'scripts': []
        }
        
        for r in self.results:
            export_data['scripts'].append({
                'script_id': r['script_id'],
                'title': r['title'],
                'author': r['author'],
                'url': r['url'],
                'version': r['version'],
                'is_strategy': r['is_strategy'],
                'is_protected': r['is_protected'],
                'has_source': bool(r.get('source_code')),
                'published_date': r.get('published_date', ''),
                'description': r.get('description', ''),
                'tags': r.get('tags', []),
                'boosts': r.get('boosts', 0),
                'views': r.get('views', 0),
                'comments': r.get('comments', 0),
                'error': r['error']
            })
        
        with open(metadata_path, 'w', encoding='utf-8') as f:
            json.dump(export_data, f, indent=2, ensure_ascii=False)
        
        print(f"\n📄 Metadata exported: {metadata_path}")

    def _print_summary(self, category: str):
        """Print final summary."""
        print(f"\n{'='*70}")
        print(f"  SUMMARY")
        print(f"{'='*70}")
        print(f"  ✓ Downloaded:          {self.stats['downloaded']}")
        print(f"  ⊘ Protected/Private:   {self.stats['skipped_protected']}")
        print(f"  ⊘ No Source Found:     {self.stats['skipped_no_code']}")
        print(f"  ✗ Failed:              {self.stats['failed']}")
        print(f"  ─────────────────────────────────")
        print(f"  Total Processed:       {len(self.results)}")
        print(f"\n  Output: {self.output_dir / sanitize_filename(category)}")
        print(f"{'='*70}\n")


async def main():
    parser = argparse.ArgumentParser(description='Download Pine Script from TradingView')
    parser.add_argument('--url', '-u', required=True, help='TradingView scripts URL')
    parser.add_argument('--output', '-o', default='./pinescript_downloads', help='Output directory')
    parser.add_argument('--max-pages', '-p', type=int, default=20, help='Max pages to scan')
    parser.add_argument('--delay', '-d', type=float, default=2.0, help='Delay between requests')
    parser.add_argument('--visible', action='store_true', help='Show browser window')
    parser.add_argument('--no-resume', action='store_true', help='Start fresh (ignore progress)')
    
    args = parser.parse_args()
    
    scraper = EnhancedTVScraper(
        output_dir=args.output,
        headless=not args.visible
    )
    
    await scraper.download_all(
        base_url=args.url,
        max_pages=args.max_pages,
        delay=args.delay,
        resume=not args.no_resume
    )


if __name__ == '__main__':
    asyncio.run(main())
