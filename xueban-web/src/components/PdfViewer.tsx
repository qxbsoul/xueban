import { useEffect, useRef, useState } from 'react';
import * as pdfjsLib from 'pdfjs-dist';
import workerSrc from 'pdfjs-dist/build/pdf.worker.min.mjs?url';

pdfjsLib.GlobalWorkerOptions.workerSrc = workerSrc;

export default function PdfViewer({ url, initialPage = 1, highlight }: { url: string; initialPage?: number; highlight?: { page: number; bbox: [number, number, number, number] } | null }) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const hostRef = useRef<HTMLDivElement | null>(null);
  const [document, setDocument] = useState<pdfjsLib.PDFDocumentProxy | null>(null);
  const [page, setPage] = useState(Math.max(1, initialPage));
  const [pageCount, setPageCount] = useState(0);
  const [zoom, setZoom] = useState(115);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [highlightStyle, setHighlightStyle] = useState<{ left: number; top: number; width: number; height: number } | null>(null);

  useEffect(() => {
    setPage(Math.max(1, initialPage));
  }, [initialPage]);

  useEffect(() => {
    let cancelled = false;
    let task: pdfjsLib.PDFDocumentLoadingTask | undefined;
    setLoading(true);
    setError('');
    setDocument(null);
    try {
      task = pdfjsLib.getDocument({ url, isEvalSupported: false });
      task.promise.then((loaded) => {
        if (cancelled) { void loaded.destroy(); return; }
        setDocument(loaded);
        setPageCount(loaded.numPages);
        setPage((value) => Math.min(Math.max(1, value), loaded.numPages));
      }).catch((reason: unknown) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : 'PDF 加载失败');
      }).finally(() => { if (!cancelled) setLoading(false); });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'PDF 加载失败');
      setLoading(false);
    }
    return () => { cancelled = true; void task?.destroy(); };
  }, [url]);

  useEffect(() => {
    if (!document || !canvasRef.current) return;
    let cancelled = false;
    let renderTask: pdfjsLib.RenderTask | undefined;
    setHighlightStyle(null);
    setLoading(true);
    document.getPage(Math.min(Math.max(1, page), document.numPages)).then((pdfPage) => {
      if (cancelled || !canvasRef.current) return;
      const base = pdfPage.getViewport({ scale: 1 });
      const hostWidth = hostRef.current?.clientWidth || 900;
      const fitScale = Math.min(1.6, Math.max(0.55, (hostWidth - 24) / base.width));
      const viewport = pdfPage.getViewport({ scale: fitScale * (zoom / 100) });
      if (highlight && highlight.page === page) {
        const [x0, y0, x1, y1] = highlight.bbox;
        const rect = viewport.convertToViewportRectangle([x0, y0, x1, y1]);
        const left = Math.min(rect[0], rect[2]);
        const top = Math.min(rect[1], rect[3]);
        setHighlightStyle({ left, top, width: Math.abs(rect[2] - rect[0]), height: Math.abs(rect[3] - rect[1]) });
      } else {
        setHighlightStyle(null);
      }
      const ratio = window.devicePixelRatio || 1;
      const canvas = canvasRef.current;
      canvas.width = Math.floor(viewport.width * ratio);
      canvas.height = Math.floor(viewport.height * ratio);
      canvas.style.width = `${Math.floor(viewport.width)}px`;
      canvas.style.height = `${Math.floor(viewport.height)}px`;
      const context = canvas.getContext('2d');
      if (!context) return;
      context.setTransform(ratio, 0, 0, ratio, 0, 0);
      renderTask = pdfPage.render({ canvasContext: context, viewport });
      return renderTask.promise;
    }).then(() => { if (!cancelled) setLoading(false); }).catch((reason: unknown) => {
      if (!cancelled) { setError(reason instanceof Error ? reason.message : '页面渲染失败'); setLoading(false); }
    });
    return () => { cancelled = true; renderTask?.cancel(); };
  }, [document, page, zoom, highlight]);

  return (
    <div className="learning-pdf-viewer">
      <div className="learning-pdf-toolbar">
        <button type="button" className="learning-btn small" disabled={page <= 1} onClick={() => setPage((value) => Math.max(1, value - 1))}>上一页</button>
        <input
          type="number"
          min={1}
          max={pageCount || undefined}
          value={page}
          onChange={(event) => setPage(Math.min(Math.max(1, Number(event.target.value) || 1), pageCount || Number.MAX_SAFE_INTEGER))}
          aria-label="PDF 页码"
        />
        <span>/ {pageCount || '…'}</span>
        <button type="button" className="learning-btn small" disabled={!pageCount || page >= pageCount} onClick={() => setPage((value) => Math.min(pageCount, value + 1))}>下一页</button>
        <button type="button" className="learning-btn small" onClick={() => setZoom((value) => Math.max(50, value - 15))}>缩小</button>
        <span>{zoom}%</span>
        <button type="button" className="learning-btn small" onClick={() => setZoom((value) => Math.min(250, value + 15))}>放大</button>
      </div>
      <div className="learning-pdf-canvas-host" ref={hostRef}>
        {loading && <div className="learning-pdf-loading"><span className="spin">◌</span> 正在渲染 PDF…</div>}
        {error && <div className="learning-pdf-error">{error}</div>}
        {!error && <div className="learning-pdf-page-wrap"><canvas ref={canvasRef} className="learning-pdf-canvas" />{highlightStyle && <span className="learning-pdf-highlight" style={highlightStyle} />}</div>}
      </div>
    </div>
  );
}