import { expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { MessageBody } from '../MessageBody'

it('renders readable emphasis and comparison tables without accepting HTML or active URLs', () => {
  const html = renderToStaticMarkup(<MessageBody text={'**对比**\n\n|手机|价格|\n|---|---|\n|A|100|\n\n<img src=x onerror=alert(1)>\n\n[支付](javascript:alert(1))\n\n![图片](https://example.com/tracker)'} />)
  expect(html).toContain('<strong>对比</strong>')
  expect(html).toContain('<table>')
  expect(html).not.toContain('<img')
  expect(html).not.toContain('href=')
  expect(html).not.toContain('onerror')
})
