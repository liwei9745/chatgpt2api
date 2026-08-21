import { ref } from 'vue'

type ConfirmOptions = {
  title?: string
  message: string
  confirmText?: string
  cancelText?: string
  checkboxLabel?: string
}

// Global singleton state: all callers share the same dialog instance.
const open = ref(false)
const title = ref('确认操作')
const message = ref('')
const confirmText = ref('确定')
const cancelText = ref('取消')
const checkboxLabel = ref('')
const checkboxChecked = ref(false)
let resolver: ((value: boolean | { confirmed: boolean; checked: boolean }) => void) | null = null

export function useConfirmDialog() {
  const ask = (options: ConfirmOptions) =>
    new Promise<boolean | { confirmed: boolean; checked: boolean }>((resolve) => {
      title.value = options.title || '确认操作'
      message.value = options.message
      confirmText.value = options.confirmText || '确定'
      cancelText.value = options.cancelText || '取消'
      checkboxLabel.value = options.checkboxLabel || ''
      checkboxChecked.value = false
      open.value = true
      resolver = resolve
    })

  const confirm = () => {
    open.value = false
    if (resolver) {
      resolver(checkboxLabel.value ? { confirmed: true, checked: checkboxChecked.value } : true)
    }
    resolver = null
  }

  const cancel = () => {
    open.value = false
    if (resolver) {
      resolver(checkboxLabel.value ? { confirmed: false, checked: false } : false)
    }
    resolver = null
  }

  return {
    open,
    title,
    message,
    confirmText,
    cancelText,
    checkboxLabel,
    checkboxChecked,
    ask,
    confirm,
    cancel,
  }
}
