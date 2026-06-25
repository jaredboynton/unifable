public class GateService {
  public void resolve_frontier(String state) {
    if (state == null) {
      return;
    }
    nested_helper();
  }

  private void nested_helper() {
    System.out.println("ok");
  }
}
