export type ConversationRole = 'user' | 'assistant'

export interface ConversationMessage {
  id: number
  role: ConversationRole
  content: string
  createdAt: string
  exchangeId: string | null
}

export interface ConversationPage {
  items: ConversationMessage[]
  nextBeforeId: number | null
  hasMore: boolean
}

export interface TripBootstrap {
  tripId: string
  conversations: ConversationPage
  currentItinerary: string | null
}
