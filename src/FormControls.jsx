import { useEffect, useId, useRef, useState } from 'react'
import { Check, ChevronDown, Minus, Plus } from 'lucide-react'

export function stepNumber(value, step = 1, min, max, direction = 1) {
  const current = value === '' || !Number.isFinite(Number(value)) ? Number(min ?? 0) : Number(value)
  const next = Number((current + Number(step) * direction).toFixed(10))
  return Math.min(max ?? Infinity, Math.max(min ?? -Infinity, next))
}

export function NumberField({ label, value, onChange, min, max, step = 1, unit, prefix, disabled = false, required = false }) {
  const canDecrease = value === '' || min == null || Number(value) > min
  const canIncrease = value === '' || max == null || Number(value) < max
  return <div className={`modern-number ${disabled ? 'is-disabled' : ''}`}>
    {prefix && <span className="modern-number-prefix">{prefix}</span>}
    <input aria-label={label} type="number" inputMode={step < 1 ? 'decimal' : 'numeric'} value={value} min={min} max={max} step={step} disabled={disabled} required={required} onChange={event => onChange(event.target.value === '' ? '' : Number(event.target.value))} />
    {unit && <span className="modern-number-unit">{unit}</span>}
    <div className="modern-stepper"><button type="button" aria-label={`Decrease ${label}`} disabled={disabled || !canDecrease} onClick={() => onChange(stepNumber(value, step, min, max, -1))}><Minus size={12} /></button><span /><button type="button" aria-label={`Increase ${label}`} disabled={disabled || !canIncrease} onClick={() => onChange(stepNumber(value, step, min, max, 1))}><Plus size={12} /></button></div>
  </div>
}

export function SelectField({ label, value, options, onChange, placeholder = 'Select', disabled = false }) {
  const [open, setOpen] = useState(false)
  const [focused, setFocused] = useState(0)
  const [above, setAbove] = useState(false)
  const root = useRef(null)
  const trigger = useRef(null)
  const listId = useId()
  const selected = options.find(option => option.value === value)

  useEffect(() => {
    if (!open) return
    const outside = event => { if (!root.current?.contains(event.target)) setOpen(false) }
    document.addEventListener('pointerdown', outside)
    return () => document.removeEventListener('pointerdown', outside)
  }, [open])
  useEffect(() => { if (disabled) setOpen(false) }, [disabled])
  useEffect(() => {
    if (open) root.current?.querySelector(`[data-option-index="${focused}"]`)?.scrollIntoView({ block: 'nearest' })
  }, [open, focused])
  const show = () => {
    const rect = root.current?.getBoundingClientRect()
    setAbove(Boolean(rect && window.innerHeight - rect.bottom < 280 && rect.top > 280))
    setFocused(Math.max(0, options.findIndex(option => option.value === value)))
    setOpen(true)
  }
  const choose = index => {
    if (!options[index]) return
    onChange(options[index].value); setOpen(false); trigger.current?.focus()
  }
  const keyboard = event => {
    if (event.key === 'Escape' || event.key === 'Tab') { setOpen(false); return }
    if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
      event.preventDefault()
      if (!open) { show(); return }
      setFocused(current => event.key === 'Home' ? 0 : event.key === 'End' ? Math.max(0, options.length - 1) : Math.min(Math.max(0, options.length - 1), Math.max(0, current + (event.key === 'ArrowDown' ? 1 : -1))))
    } else if ((event.key === 'Enter' || event.key === ' ') && open) {
      event.preventDefault(); choose(focused)
    }
  }
  return <div className={`modern-select ${open ? 'is-open' : ''}`} ref={root}>
    <button ref={trigger} type="button" role="combobox" aria-label={label} aria-haspopup="listbox" aria-expanded={open} aria-controls={listId} aria-activedescendant={open && options.length ? `${listId}-${focused}` : undefined} className={`modern-select-trigger ${selected ? '' : 'is-placeholder'}`} disabled={disabled} onClick={() => open ? setOpen(false) : show()} onKeyDown={keyboard}><span>{selected?.label || placeholder}</span><ChevronDown size={16} /></button>
    {open && <div className={`modern-select-menu ${above ? 'opens-above' : ''}`} id={listId} role="listbox" aria-label={label}>
      {options.length ? options.map((option, index) => <div key={option.value} id={`${listId}-${index}`} role="option" aria-selected={option.value === value} data-option-index={index} className={`modern-select-option ${focused === index ? 'is-focused' : ''} ${option.value === value ? 'is-selected' : ''}`} onPointerMove={() => setFocused(index)} onPointerDown={event => event.preventDefault()} onClick={() => choose(index)}><span>{option.label}{option.description && <small>{option.description}</small>}</span>{option.value === value && <Check size={15} />}</div>) : <div className="modern-select-empty">No saved strategies</div>}
    </div>}
  </div>
}

// Keep visited panels mounted so requests, results, and form edits survive
// sidebar navigation. A panel starts work only after its first visit.
export function RetainedPanel({ active, children, name }) {
  const [visited, setVisited] = useState(active)
  useEffect(() => { if (active) setVisited(true) }, [active])
  if (!active && !visited) return null
  return <div className="dashboard-panel" hidden={!active} aria-label={name}>{children}</div>
}
