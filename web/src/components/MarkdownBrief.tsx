import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

interface Props {
  md: string;
}

export function MarkdownBrief({ md }: Props) {
  return (
    <div className="content-panel markdown-brief">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{md}</ReactMarkdown>
    </div>
  );
}
