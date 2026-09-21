import { noteResources } from './note-resources.js';

const mediaHosts = new Set(['upload.wikimedia.org', 'thumb.wikimedia.org']);

function safeMediaURL(value) {
  if (typeof value !== 'string' || value.length > 1800 || /[\s\\\x00-\x1f]/.test(value)) return false;
  try {
    const url = new URL(value), path = decodeURIComponent(url.pathname);
    return url.protocol === 'https:' && mediaHosts.has(url.hostname) && !url.username && !url.password && !url.port && !url.hash
      && path.startsWith('/wikipedia/commons/') && /\.(?:png|jpe?g|webp)$/i.test(path)
      && !path.split('/').some(part => part === '.' || part === '..') && !/[\\\x00-\x1f]/.test(path);
  } catch { return false; }
}

function safeSource(value, license = false) {
  if (typeof value !== 'string' || value.length > 1800 || /[\s\\\x00-\x1f]/.test(value)) return false;
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && !url.username && !url.password && !url.port
      && (url.hostname === 'commons.wikimedia.org' || (license && url.hostname === 'creativecommons.org'))
      && (license || url.pathname.startsWith('/wiki/File:'));
  } catch { return false; }
}

function validateResource(entry) {
  if (!entry || typeof entry !== 'object' || Array.isArray(entry) || !safeMediaURL(entry.imagePath)
    || !safeSource(entry.url) || !safeSource(entry.licenseUrl, true)) return null;
  const result = { imagePath: entry.imagePath, url: entry.url, licenseUrl: entry.licenseUrl };
  for (const [key, maximum] of [['title', 240], ['author', 600], ['license', 160], ['credit', 1200], ['query', 100]]) {
    const value = entry[key] ?? '';
    if (typeof value !== 'string' || value.length > maximum || (['title', 'author', 'license'].includes(key) && !value.trim())) return null;
    if (value) result[key] = value;
  }
  for (const key of ['width', 'height']) if (Number.isInteger(entry[key]) && entry[key] > 0 && entry[key] <= 100000) result[key] = entry[key];
  return result;
}

// Credits travel with each Markdown version and remain isolated to that note.
export function parseNoteImageResources(markdown) {
  const resources = new Map();
  for (const match of String(markdown || '').replace(/\r\n?/g, '\n').matchAll(/^```note-images[ \t]*\n([\s\S]*?)\n```[ \t]*(?:\n|$)/gm)) {
    if (match[1].length > 32000) continue;
    try {
      const entries = JSON.parse(match[1]);
      if (!Array.isArray(entries)) continue;
      for (const entry of entries.slice(0, 8)) {
        const resource = validateResource(entry);
        if (resource && resources.size < 8) resources.set(resource.imagePath, resource);
      }
    } catch { /* Invalid metadata never grants permission to fetch an image. */ }
  }
  return resources;
}

export function getNoteResource(destination, resources = new Map()) {
  if (typeof destination !== 'string') return null;
  const path = '/' + destination.replace(/^\.\//, '').replace(/^\//, '');
  if (Object.hasOwn(noteResources, path)) return { ...noteResources[path], imagePath: path };
  return safeMediaURL(destination) && resources instanceof Map ? resources.get(destination) || null : null;
}
