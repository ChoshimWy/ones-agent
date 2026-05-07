import { Button } from "@/components/ui/button";
import { FileQuestion } from "lucide-react";
import { useNavigate } from "react-router-dom";

export default function NotFoundPage() {
  const navigate = useNavigate();
  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-4 text-center">
      <FileQuestion className="h-16 w-16 text-muted-foreground" />
      <h1 className="text-2xl font-bold">404 Not Found</h1>
      <p className="text-muted-foreground">
        The page you're looking for doesn't exist.
      </p>
      <Button variant="outline" onClick={() => navigate("/")}>
        Go home
      </Button>
    </div>
  );
}
