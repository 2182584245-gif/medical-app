import { common } from '@kit.AbilityKit'
import { uniformTypeDescriptor } from '@kit.ArkData'
import { systemShare } from '@kit.ShareKit'

// Opening or dismissing the panel does not confirm a share. Only its completion event does.
export async function shareText(context: common.UIAbilityContext, title: string, text: string): Promise<boolean> {
  const data = new systemShare.SharedData({
    utd: uniformTypeDescriptor.UniformDataType.PLAIN_TEXT,
    title: title,
    content: text
  })
  const controller = new systemShare.ShareController(data)
  let dismiss: (() => void) | undefined
  try {
    return await new Promise<boolean>((resolve, reject) => {
      dismiss = () => resolve(false)
      controller.on('shareCompleted', () => resolve(true))
      controller.on('dismiss', dismiss)
      controller.show(context, { selectionMode: systemShare.SelectionMode.SINGLE }).catch((error: Error) => reject(error))
    })
  } finally {
    controller.off('shareCompleted')
    if (dismiss) controller.off('dismiss', dismiss)
  }
}
