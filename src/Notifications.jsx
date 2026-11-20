import { useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { AlertTriangle, Bell, BellOff, Check, CheckCheck, Circle, Info, X } from 'lucide-react'

const filters = [['all', 'All'], ['unread', 'Unread'], ['read', 'Read']]
const date = new Intl.DateTimeFormat('en-IN', {
  timeZone: 'Asia/Kolkata', dateStyle: 'medium',
})
const clock = new Intl.DateTimeFormat('en-IN', {
  timeZone: 'Asia/Kolkata', hour: 'numeric', minute: '2-digit',
})

function readIds(value) {
  try {
    const entries = JSON.parse(value || '[]')
    return Array.isArray(entries) ? entries.filter(id => typeof id === 'string').slice(-600) : []
  } catch { return [] }
}

export function useNotificationInbox(accountId, events) {
  const key = accountId ? `autovest:notifications:read:${accountId}` : null
  const [saved, setSaved] = useState({ key: null, ids: [] })
  useEffect(() => {
    let ids = []
    if (key) {
      try {
        ids = readIds(localStorage.getItem(key))
      } catch { /* Browser storage can be unavailable. */ }
    }
    setSaved({ key, ids })
    if (!key) return
    function sync(event) {
      if (event.key === key) setSaved({ key, ids: readIds(event.newValue) })
    }
    window.addEventListener('storage', sync)
    return () => window.removeEventListener('storage', sync)
  }, [key])
  const read = useMemo(() => new Set(saved.key === key ? saved.ids : []), [saved, key])
  function save(next) {
    setSaved({ key, ids: next })
    if (key) {
      try { localStorage.setItem(key, JSON.stringify(next)) } catch { /* Read state stays available in memory. */ }
    }
  }
  return {
    read,
    markRead: ids => save([...new Set([...read, ...ids])].slice(-600)),
    markUnread: id => save([...read].filter(item => item !== id)),
    unread: events.filter(event => !read.has(event.id)).length,
  }
}

export function InfoTip({ text }) {
  return <span className="info-tip">
    <button type="button" aria-label={text} className="info-tip-trigger"><Info size={15} /></button>
    <span className="info-tip-text" aria-hidden="true">{text}</span>
  </span>
}

export function NotificationInbox({ open, setOpen, events, read, markRead, markUnread, unread, error }) {
  const [filter, setFilter] = useState('all')
  const root = useRef(null)
  const panel = useRef(null)
  const trigger = useRef(null)
  const closeButton = useRef(null)
  const returnFocus = useRef(null)
  const selectedFilter = useRef(null)

  function close() {
    setOpen(false)
    const opener = returnFocus.current || trigger.current
    opener?.focus()
  }

  useEffect(() => {
    if (!open) return
    returnFocus.current = document.activeElement instanceof HTMLElement && document.activeElement !== document.body
      ? document.activeElement : trigger.current
    closeButton.current?.focus()
    function onKey(event) {
      if (event.key === 'Escape') {
        event.preventDefault()
        setOpen(false)
        const opener = returnFocus.current || trigger.current
        opener?.focus()
      }
    }
    function onOutside(event) {
      if (!root.current?.contains(event.target) && !panel.current?.contains(event.target)
        && !event.target.closest?.('[data-inbox-trigger]')) setOpen(false)
    }
    document.addEventListener('keydown', onKey)
    document.addEventListener('pointerdown', onOutside)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.removeEventListener('pointerdown', onOutside)
    }
  }, [open, setOpen])

  const visible = events.filter(event => filter === 'all' || read.has(event.id) === (filter === 'read'))
  const today = date.format(new Date())
  const yesterday = date.format(new Date(Date.now() - 86400000))
  const groups = new Map()
  for (const event of visible) {
    const time = new Date(event.at)
    const label = Number.isFinite(time.getTime()) ? date.format(time) : 'Earlier'
    if (!groups.has(label)) groups.set(label, [])
    groups.get(label).push(event)
  }
  const counts = { all: events.length, unread, read: events.length - unread }

  function toggleRead(event, isRead) {
    if (isRead) markUnread(event.id)
    else markRead([event.id])
    if (filter !== 'all') selectedFilter.current?.focus()
  }

  return <div className="notification-anchor" ref={root}>
    <button ref={trigger} type="button" className="icon-button inbox-trigger" aria-label={unread ? `Notifications, ${unread} unread` : 'Notifications'}
      aria-expanded={open} aria-controls={open ? 'workspace-notifications' : undefined} onClick={() => setOpen(!open)}>
      <Bell size={19} />{unread > 0 && <span className="inbox-count" aria-hidden="true">{unread > 99 ? '99+' : unread}</span>}
    </button>
    {open && createPortal(<section ref={panel} className="notification-inbox" role="dialog" id="workspace-notifications" aria-labelledby="inbox-heading">
      <header className="inbox-header">
        <span className="inbox-heading-icon"><Bell size={20} /></span>
        <div><h2 id="inbox-heading">Notifications</h2><p>{unread ? `${unread} unread` : 'All caught up'}</p></div>
        <button ref={closeButton} type="button" className="icon-button inbox-close" aria-label="Close notifications" onClick={close}><X size={18} /></button>
      </header>
      <div className="inbox-controls">
        <div className="inbox-filters" role="group" aria-label="Filter notifications">
          {filters.map(([value, text]) => <button key={value} ref={filter === value ? selectedFilter : undefined}
            type="button" aria-pressed={filter === value} onClick={() => setFilter(value)}>{text}<span>{counts[value]}</span></button>)}
        </div>
        <button type="button" className="inbox-mark" disabled={!unread} onClick={() => markRead(events.map(event => event.id))}><CheckCheck size={16} /> Mark all read</button>
      </div>
      {error && <p className="inbox-error" role="status">{error}</p>}
      <div className="inbox-feed">
        {visible.length ? [...groups].map(([label, items]) => <div className="inbox-group" key={label}>
          <h3>{label === today ? 'Today' : label === yesterday ? 'Yesterday' : label}</h3>
          {items.map(event => {
            const isRead = read.has(event.id)
            const time = new Date(event.at)
            const Icon = ['error', 'warning'].includes(event.level) ? AlertTriangle : event.level === 'success' ? Check : Info
            return <article className={`inbox-event ${isRead ? 'is-read' : 'is-unread'} ${event.level || 'info'}`} key={event.id}>
              <span className="inbox-event-icon"><Icon size={16} /></span>
              <div><strong>{event.title}</strong><p>{event.message}</p>
                {Number.isFinite(time.getTime()) && <time dateTime={time.toISOString()} title={`${date.format(time)} · ${clock.format(time)} IST`}>{clock.format(time)} IST</time>}
              </div>
              <button type="button" className={`icon-button ${isRead ? 'inbox-unread-one' : 'inbox-read-one'}`}
                aria-label={`Mark ${event.title} as ${isRead ? 'unread' : 'read'}`} title={isRead ? 'Mark as unread' : 'Mark as read'} onClick={() => toggleRead(event, isRead)}>
                {isRead ? <Circle size={14} /> : <Check size={16} />}
              </button>
            </article>
          })}
        </div>) : <div className="inbox-empty"><BellOff size={21} /><p>{filter === 'unread' ? 'No unread notifications' : filter === 'read' ? 'No read notifications' : 'No notifications yet'}</p></div>}
      </div>
    </section>, document.body)}
  </div>
}
