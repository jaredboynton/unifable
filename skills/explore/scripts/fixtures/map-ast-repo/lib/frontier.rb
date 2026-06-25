module Frontier
  def self.adjudicate_dispute(task)
    task || :fail_open
  end

  class Nested
    def resolve_inner
      true
    end
  end
end
