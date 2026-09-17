import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

/** Read-only prose: no raw HTML, remote images, or model-generated action links. */
export function MessageBody({ text }: { text: string }) {
  return <div className="message-copy markdown-copy">
    <Markdown remarkPlugins={[remarkGfm]} skipHtml components={{
      a: ({ children }) => <span>{children}</span>,
      img: ({ alt }) => <span>{alt || ''}</span>,
      table: ({ children }) => <div className="message-table"><table>{children}</table></div>,
    }}>{text}</Markdown>
  </div>
}
