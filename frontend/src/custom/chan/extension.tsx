import { GitBranch, Search } from 'lucide-react'
import type { FrontendExtension } from '@/extensions/types'
import { ChanPage } from './ChanPage'
import { ChanScreenPage } from './ChanScreenPage'

const extension: FrontendExtension = {
  id: 'chan.analysis',
  apiVersion: 1,
  routes: [
    { id: 'chan-structure', path: '/chan', component: ChanPage },
    { id: 'chan-screen', path: '/chan/screen', component: ChanScreenPage },
  ],
  navigation: [
    { id: 'chan-structure', routeId: 'chan-structure', label: '缠论结构', icon: GitBranch },
    { id: 'chan-screen', routeId: 'chan-screen', label: '缠论选股', icon: Search },
  ],
}

export default extension
