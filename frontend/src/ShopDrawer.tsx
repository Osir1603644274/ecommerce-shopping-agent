import { useEffect, useRef, type ReactNode } from 'react'
import { X } from 'lucide-react'

export function ShopDrawer({ title, onClose, children, open = true }: {title:string; onClose:()=>void; children:ReactNode; open?:boolean}) {
  const ref=useRef<HTMLDialogElement>(null)
  useEffect(()=>{const node=ref.current;if(open)node?.showModal();else node?.close();return()=>node?.close()},[open])
  return <dialog ref={ref} className="shop-drawer" aria-label={title} onCancel={onClose}>
    <header><h2>{title}</h2><button className="icon-button" aria-label={`关闭${title}`} onClick={onClose}><X size={20}/></button></header>
    <div className="drawer-content">{children}</div>
  </dialog>
}
